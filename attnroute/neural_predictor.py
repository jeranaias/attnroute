#!/usr/bin/env python3
"""
Neural File Predictor — GRU-based sequence model for file access prediction.

**EXPERIMENTAL**: This module requires PyTorch and Model2Vec. It currently
overfits on small datasets (~10 sessions) because file indices are project-specific
and don't transfer across codebases. Needs either: (a) much more training data
(500+ sessions), (b) feature-based file representation instead of index-based,
or (c) external datasets like KaVE or GH Archive.

Learns from Claude Code session transcripts to predict which files will be
accessed next based on:
  - Recent file access sequence (last 10 files)
  - Prompt text embedding (via Model2Vec potion-base-8M)
  - File co-access patterns

Architecture:
  - Embedding: File path → learned embedding (64-dim)
  - Prompt encoder: Model2Vec → linear projection (256 → 64)
  - Sequence model: GRU (128 hidden, 1 layer)
  - Output: Probability distribution over all known files

Training data: Claude Code JSONL transcripts (~/.claude/projects/)

Usage:
    # Train
    python -m attnroute.neural_predictor train

    # Predict
    from attnroute.neural_predictor import NeuralPredictor
    predictor = NeuralPredictor()
    predictions = predictor.predict(prompt="fix the auth bug", recent_files=[...])
"""

import json
import pickle
import sys
from collections import Counter
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

# Paths
PROJECTS_DIR = Path.home() / ".claude" / "projects"
MODEL_PATH = Path.home() / ".claude" / "telemetry" / "neural_predictor.pt"
VOCAB_PATH = Path.home() / ".claude" / "telemetry" / "neural_predictor_vocab.pkl"

# Hyperparameters
FILE_EMBED_DIM = 64
PROMPT_DIM = 64
GRU_HIDDEN = 128
SEQ_LEN = 10          # Look at last 10 files
MAX_VOCAB = 2000      # Max unique files to track
BATCH_SIZE = 32
EPOCHS = 15
LR = 0.001
PROMPT_FEATURE_DIM = 256  # Model2Vec output dimension (potion-base-8M)


# ============================================================================
# DATA EXTRACTION
# ============================================================================

def normalize_path(path: str) -> str:
    """Normalize path for consistent vocabulary."""
    path = path.replace("\\", "/")
    home = str(Path.home()).replace("\\", "/")
    if path.lower().startswith(home.lower()):
        path = path[len(home):].lstrip("/")
    # Git Bash style
    if len(home) >= 2 and home[1] == ":":
        git_home = f"/{home[0].lower()}{home[2:]}"
        if path.lower().startswith(git_home.lower()):
            path = path[len(git_home):].lstrip("/")
    return path


def extract_training_data(max_sessions: int = 200) -> list[dict]:
    """
    Extract file access sequences from Claude Code transcripts.

    Extracts two types of training data:
    1. turns: [{prompt, files}] — prompt→file associations
    2. file_sequences: [path, path, ...] — sequential file access order

    The sequence data is the primary signal for the GRU — it learns
    which file typically comes after which.
    """
    sessions = []

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for session_file in project_dir.glob("*.jsonl"):
            if len(sessions) >= max_sessions:
                break
            if session_file.stat().st_size < 1000:
                continue

            try:
                with open(session_file, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except Exception:
                continue

            turns = []
            file_sequence = []  # All files accessed in order
            current_prompt = None
            current_files = []

            for line in lines:
                try:
                    entry = json.loads(line.strip())
                except (json.JSONDecodeError, ValueError):
                    continue

                if entry.get("type") == "user":
                    # Save previous turn if it had files
                    if current_files:
                        prompt_text = current_prompt or ""
                        turns.append({
                            "prompt": prompt_text,
                            "files": current_files,
                        })
                        file_sequence.extend(current_files)

                    # Extract prompt text from any format
                    message = entry.get("message", {})
                    content = message.get("content", "")
                    if isinstance(content, str):
                        # Only use actual user prompts, not tool results
                        if content.strip() and not content.startswith("<"):
                            current_prompt = content
                    elif isinstance(content, list):
                        texts = []
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                t = c.get("text", "")
                                if t.strip() and not t.startswith("<"):
                                    texts.append(t)
                        if texts:
                            current_prompt = " ".join(texts)
                    current_files = []

                elif entry.get("type") == "assistant":
                    content = entry.get("message", {}).get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_use":
                                name = block.get("name", "")
                                inp = block.get("input", {})
                                if name in ("Read", "Edit", "Write"):
                                    fp = inp.get("file_path", "")
                                    if fp:
                                        norm = normalize_path(fp)
                                        if norm not in current_files:
                                            current_files.append(norm)

            # Don't forget last turn
            if current_files:
                turns.append({
                    "prompt": current_prompt or "",
                    "files": current_files,
                })
                file_sequence.extend(current_files)

            # Accept sessions with at least some file access data
            if len(file_sequence) >= 5:
                sessions.append({
                    "turns": turns,
                    "file_sequence": file_sequence,
                })

        if len(sessions) >= max_sessions:
            break

    return sessions


def build_vocabulary(sessions: list[dict], max_vocab: int = MAX_VOCAB) -> dict:
    """
    Build file vocabulary from training data.

    Returns {normalized_path: index} mapping.
    """
    file_counts = Counter()
    for session in sessions:
        for turn in session["turns"]:
            for f in turn["files"]:
                file_counts[f] += 1

    # Keep most common files + special tokens
    vocab = {"<PAD>": 0, "<UNK>": 1}
    for path, _ in file_counts.most_common(max_vocab - 2):
        vocab[path] = len(vocab)

    return vocab


# ============================================================================
# PROMPT FEATURES
# ============================================================================

_model2vec = None
_model2vec_failed = False


def _get_model2vec():
    """Lazy-load Model2Vec for prompt encoding."""
    global _model2vec, _model2vec_failed
    if _model2vec_failed:
        return None
    if _model2vec is not None:
        return _model2vec
    try:
        from model2vec import StaticModel
        _model2vec = StaticModel.from_pretrained("minishlab/potion-base-8M")
        return _model2vec
    except Exception:
        _model2vec_failed = True
        return None


def encode_prompt(prompt: str) -> torch.Tensor:
    """
    Encode a prompt into a fixed-size feature vector.

    Uses Model2Vec if available, falls back to bag-of-words hashing.
    """
    m2v = _get_model2vec()
    if m2v is not None:
        try:
            embedding = m2v.encode(prompt)
            # Model2Vec returns numpy array, convert to tensor
            return torch.tensor(embedding, dtype=torch.float32)
        except Exception:
            pass

    # Fallback: simple hash-based bag of words (256-dim)
    import hashlib
    words = prompt.lower().split()
    vec = torch.zeros(PROMPT_FEATURE_DIM, dtype=torch.float32)
    for word in words:
        h = int(hashlib.md5(word.encode()).hexdigest(), 16) % PROMPT_FEATURE_DIM
        vec[h] += 1.0
    # Normalize
    norm = vec.norm()
    if norm > 0:
        vec = vec / norm
    return vec


# ============================================================================
# DATASET
# ============================================================================

class FileSequenceDataset(Dataset):
    """
    Next-file prediction dataset.

    For each file access, the input is the previous SEQ_LEN files.
    The target is the NEXT file accessed (single class, cross-entropy).

    This creates much more training data from the same sessions —
    every file access is a training example.
    """

    def __init__(self, sessions: list[dict], vocab: dict, seq_len: int = SEQ_LEN):
        self.samples = []
        self.vocab = vocab
        self.seq_len = seq_len
        self.num_files = len(vocab)

        print(f"  Building dataset from {len(sessions)} sessions...", flush=True)

        for session in sessions:
            file_seq = session.get("file_sequence", [])
            if len(file_seq) < 2:
                continue

            # Create a training sample for each file in the sequence
            for i in range(1, len(file_seq)):
                target_file = file_seq[i]
                target_idx = vocab.get(target_file, 1)
                if target_idx <= 1:  # Skip PAD/UNK targets
                    continue

                # Input: previous seq_len files
                history = file_seq[max(0, i - seq_len):i]
                seq = [vocab.get(f, 1) for f in history]
                while len(seq) < seq_len:
                    seq.insert(0, 0)  # Pad at beginning

                # Find the prompt associated with this file access
                # (approximate: use the turn whose files include this file)
                prompt_text = ""
                for turn in session["turns"]:
                    if target_file in turn.get("files", []):
                        prompt_text = turn.get("prompt", "")
                        break

                prompt_feat = encode_prompt(prompt_text)

                self.samples.append((
                    torch.tensor(seq, dtype=torch.long),
                    prompt_feat,
                    torch.tensor(target_idx, dtype=torch.long),
                ))

        print(f"  Dataset: {len(self.samples)} samples, {self.num_files} file classes", flush=True)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        return self.samples[idx]


# ============================================================================
# MODEL
# ============================================================================

class FileGRU(nn.Module):
    """
    GRU-based file access predictor.

    Input: sequence of file indices + prompt embedding
    Output: probability distribution over all known files
    """

    def __init__(self, vocab_size: int, prompt_dim: int = PROMPT_FEATURE_DIM):
        super().__init__()
        self.vocab_size = vocab_size

        # File embedding
        self.file_embed = nn.Embedding(vocab_size, FILE_EMBED_DIM, padding_idx=0)

        # GRU over file sequence
        self.gru = nn.GRU(
            input_size=FILE_EMBED_DIM,
            hidden_size=GRU_HIDDEN,
            num_layers=1,
            batch_first=True,
        )

        # Prompt projection
        self.prompt_proj = nn.Linear(prompt_dim, PROMPT_DIM)

        # Output head: concat(GRU_output, prompt_proj) → file probabilities
        self.output = nn.Sequential(
            nn.Linear(GRU_HIDDEN + PROMPT_DIM, 256),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(256, vocab_size),
        )

    def forward(self, file_seq: torch.Tensor, prompt_feat: torch.Tensor) -> torch.Tensor:
        """
        Args:
            file_seq: (batch, seq_len) — file index sequence
            prompt_feat: (batch, prompt_dim) — prompt embedding

        Returns:
            (batch, vocab_size) — logits per file
        """
        # Embed file sequence
        embedded = self.file_embed(file_seq)  # (batch, seq_len, embed_dim)

        # GRU
        gru_out, _ = self.gru(embedded)  # (batch, seq_len, hidden)
        last_hidden = gru_out[:, -1, :]  # (batch, hidden) — last timestep

        # Prompt
        prompt_proj = F.relu(self.prompt_proj(prompt_feat))  # (batch, prompt_dim)

        # Combine and predict
        combined = torch.cat([last_hidden, prompt_proj], dim=1)
        logits = self.output(combined)

        return logits


class FileGRUv2(nn.Module):
    """Feature-based GRU that generalizes across projects.

    Instead of file index -> embedding (project-specific),
    uses file feature vectors -> embedding (project-agnostic).
    Output is binary per-candidate, not classification over vocabulary.

    EXPERIMENTAL: Requires feature_extractor and more training data.
    """

    FILE_FEATURES = 12  # From data.feature_extractor

    def __init__(self, prompt_dim: int = PROMPT_FEATURE_DIM):
        super().__init__()

        # File feature encoder (replaces nn.Embedding)
        self.file_encoder = nn.Sequential(
            nn.Linear(self.FILE_FEATURES, FILE_EMBED_DIM),
            nn.ReLU(),
        )

        # GRU over encoded file features
        self.gru = nn.GRU(
            input_size=FILE_EMBED_DIM,
            hidden_size=GRU_HIDDEN,
            num_layers=1,
            batch_first=True,
        )

        # Prompt projection
        self.prompt_proj = nn.Linear(prompt_dim, PROMPT_DIM)

        # Candidate scorer: given context + candidate features, predict relevance
        self.scorer = nn.Sequential(
            nn.Linear(GRU_HIDDEN + PROMPT_DIM + FILE_EMBED_DIM, 128),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(128, 1),
        )

    def forward(
        self,
        file_seq_features: torch.Tensor,
        prompt_feat: torch.Tensor,
        candidate_features: torch.Tensor,
    ) -> torch.Tensor:
        """
        Args:
            file_seq_features: (batch, seq_len, 12) — feature vectors for recent files
            prompt_feat: (batch, prompt_dim) — prompt embedding
            candidate_features: (batch, num_candidates, 12) — features for candidate files

        Returns:
            (batch, num_candidates) — relevance scores
        """
        # Encode file sequence
        encoded_seq = self.file_encoder(file_seq_features)  # (batch, seq_len, embed_dim)
        gru_out, _ = self.gru(encoded_seq)
        context = gru_out[:, -1, :]  # (batch, hidden)

        # Prompt
        prompt_proj = F.relu(self.prompt_proj(prompt_feat))  # (batch, prompt_dim)

        # Score each candidate
        batch_size, num_candidates, _ = candidate_features.shape
        encoded_cands = self.file_encoder(candidate_features)  # (batch, num_cands, embed_dim)

        # Expand context to match candidates
        ctx_expanded = context.unsqueeze(1).expand(-1, num_candidates, -1)
        prompt_expanded = prompt_proj.unsqueeze(1).expand(-1, num_candidates, -1)

        combined = torch.cat([ctx_expanded, prompt_expanded, encoded_cands], dim=2)
        scores = self.scorer(combined).squeeze(-1)  # (batch, num_candidates)

        return scores


# ============================================================================
# TRAINING
# ============================================================================

def train_model(max_sessions: int = 200, epochs: int = EPOCHS) -> tuple:
    """
    Train the neural file predictor.

    Returns (model, vocab, metrics).
    """
    print("=" * 60)
    print("NEURAL FILE PREDICTOR — Training")
    print("=" * 60)
    print()

    # Extract data
    print("  Extracting training data from Claude Code sessions...")
    sessions = extract_training_data(max_sessions)
    total_turns = sum(len(s["turns"]) for s in sessions)
    total_files = sum(len(s.get("file_sequence", [])) for s in sessions)
    print(f"  Found {len(sessions)} sessions, {total_turns} turns, {total_files} file accesses")

    if len(sessions) < 3:
        print("  Not enough sessions to train. Need at least 3.")
        return None, None, None

    # Build vocabulary
    vocab = build_vocabulary(sessions)
    print(f"  Vocabulary: {len(vocab)} files")

    # Split train/val (80/20)
    split = max(1, int(len(sessions) * 0.8))
    train_sessions = sessions[:split]
    val_sessions = sessions[split:] if split < len(sessions) else sessions[-1:]

    # Check actual prompt dim from a sample
    sample_prompt = encode_prompt("test prompt")
    prompt_dim = sample_prompt.shape[0]
    print(f"  Prompt feature dimension: {prompt_dim}")

    # Build datasets
    train_dataset = FileSequenceDataset(train_sessions, vocab)
    val_dataset = FileSequenceDataset(val_sessions, vocab)

    if len(train_dataset) < 10:
        print("  Not enough training samples.")
        return None, None, None

    train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=BATCH_SIZE, shuffle=False)

    # Model
    model = FileGRU(vocab_size=len(vocab), prompt_dim=prompt_dim)
    param_count = sum(p.numel() for p in model.parameters())
    print(f"  Model parameters: {param_count:,}")
    print()

    # Training with cross-entropy (next-file prediction)
    optimizer = torch.optim.Adam(model.parameters(), lr=LR)
    criterion = nn.CrossEntropyLoss(ignore_index=0)  # Ignore PAD targets

    best_val_acc = 0
    best_state = None
    metrics_history = []

    for epoch in range(epochs):
        # Train
        model.train()
        train_loss = 0
        train_correct = 0
        train_total = 0
        for file_seq, prompt_feat, target in train_loader:
            optimizer.zero_grad()
            logits = model(file_seq, prompt_feat)
            loss = criterion(logits, target)
            loss.backward()
            optimizer.step()
            train_loss += loss.item()

            # Top-1 accuracy
            preds = logits.argmax(dim=1)
            train_correct += (preds == target).sum().item()
            train_total += target.shape[0]

        train_loss /= len(train_loader)
        train_acc = train_correct / max(1, train_total)

        # Validate
        model.eval()
        val_loss = 0
        val_top1 = 0
        val_top5 = 0
        val_total = 0

        with torch.no_grad():
            for file_seq, prompt_feat, target in val_loader:
                logits = model(file_seq, prompt_feat)
                loss = criterion(logits, target)
                val_loss += loss.item()

                # Top-1 accuracy
                preds = logits.argmax(dim=1)
                val_top1 += (preds == target).sum().item()

                # Top-5 accuracy
                top5 = torch.topk(logits, k=min(5, logits.shape[1]), dim=1).indices
                for i in range(target.shape[0]):
                    if target[i].item() in top5[i].tolist():
                        val_top5 += 1

                val_total += target.shape[0]

        val_loss /= max(1, len(val_loader))
        top1_acc = val_top1 / max(1, val_total)
        top5_acc = val_top5 / max(1, val_total)

        metrics_history.append({
            "epoch": epoch + 1,
            "train_loss": train_loss,
            "val_loss": val_loss,
            "train_acc": train_acc,
            "top1_acc": top1_acc,
            "top5_acc": top5_acc,
        })

        print(f"  Epoch {epoch + 1:>2}/{epochs}  "
              f"loss={train_loss:.4f}/{val_loss:.4f}  "
              f"train_acc={train_acc:.1%}  "
              f"top1={top1_acc:.1%} top5={top5_acc:.1%}")

        if top5_acc > best_val_acc:
            best_val_acc = top5_acc
            best_state = model.state_dict().copy()

    # Restore best model
    if best_state is not None:
        model.load_state_dict(best_state)

    print(f"\n  Best validation top-5 accuracy: {best_val_acc:.1%}")
    print(f"  Parameters: {param_count:,}")

    return model, vocab, metrics_history


def save_neural_model(model: nn.Module, vocab: dict):
    """Save trained model and vocabulary."""
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), MODEL_PATH)
    with open(VOCAB_PATH, "wb") as f:
        pickle.dump(vocab, f)
    print(f"  Model saved to: {MODEL_PATH}")
    print(f"  Vocab saved to: {VOCAB_PATH}")


def load_neural_model() -> tuple:
    """Load trained model and vocabulary."""
    if not MODEL_PATH.exists() or not VOCAB_PATH.exists():
        return None, None

    try:
        with open(VOCAB_PATH, "rb") as f:
            vocab = pickle.load(f)

        # Detect prompt dim
        sample = encode_prompt("test")
        prompt_dim = sample.shape[0]

        model = FileGRU(vocab_size=len(vocab), prompt_dim=prompt_dim)
        model.load_state_dict(torch.load(MODEL_PATH, weights_only=True))
        model.eval()
        return model, vocab
    except Exception as e:
        print(f"  Failed to load neural model: {e}", file=sys.stderr)
        return None, None


# ============================================================================
# PREDICTION
# ============================================================================

class NeuralPredictor:
    """
    Production wrapper for neural file prediction.

    Lazily loads the trained model. Falls back gracefully if no model exists.
    """

    def __init__(self):
        self._model = None
        self._vocab = None
        self._reverse_vocab = None
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        self._loaded = True
        self._model, self._vocab = load_neural_model()
        if self._vocab:
            self._reverse_vocab = {v: k for k, v in self._vocab.items()}

    def predict(
        self,
        prompt: str,
        recent_files: list[str] = None,
        top_k: int = 10,
    ) -> list[tuple[str, float]]:
        """
        Predict which files will be accessed next.

        Args:
            prompt: User's prompt text
            recent_files: List of recently accessed file paths
            top_k: Number of predictions to return

        Returns:
            List of (file_path, probability) tuples, sorted by probability
        """
        self._ensure_loaded()
        if self._model is None:
            return []

        recent_files = recent_files or []

        # Build file sequence input
        seq = [self._vocab.get(f, 1) for f in recent_files[-SEQ_LEN:]]
        while len(seq) < SEQ_LEN:
            seq.insert(0, 0)  # Pad
        seq_tensor = torch.tensor([seq], dtype=torch.long)

        # Encode prompt
        prompt_feat = encode_prompt(prompt).unsqueeze(0)

        # Predict
        with torch.no_grad():
            logits = self._model(seq_tensor, prompt_feat)
            probs = torch.sigmoid(logits).squeeze(0)

        # Get top-k predictions
        top_vals, top_idxs = torch.topk(probs, k=min(top_k, len(probs)))

        results = []
        for prob, idx in zip(top_vals.tolist(), top_idxs.tolist()):
            if idx in (0, 1):  # Skip PAD and UNK
                continue
            path = self._reverse_vocab.get(idx, None)
            if path:
                results.append((path, prob))

        return results

    @property
    def available(self) -> bool:
        """Check if model is loaded and ready."""
        self._ensure_loaded()
        return self._model is not None


# ============================================================================
# BENCHMARK
# ============================================================================

def benchmark_neural(model: nn.Module, vocab: dict, max_turns: int = 1500):
    """Benchmark the neural predictor against test data."""
    print("\n" + "=" * 60)
    print("NEURAL FILE PREDICTOR — Benchmark")
    print("=" * 60)

    sessions = extract_training_data(max_sessions=200)
    # Use last 20% as test
    split = max(1, int(len(sessions) * 0.8))
    test_sessions = sessions[split:]

    model.eval()

    top1_hits = 0
    top5_hits = 0
    total = 0

    for session in test_sessions:
        file_seq = session.get("file_sequence", [])
        if len(file_seq) < 2:
            continue

        for i in range(1, len(file_seq)):
            target = file_seq[i]
            target_idx = vocab.get(target, 1)
            if target_idx <= 1:
                continue

            # Input
            history = file_seq[max(0, i - SEQ_LEN):i]
            seq = [vocab.get(f, 1) for f in history]
            while len(seq) < SEQ_LEN:
                seq.insert(0, 0)
            seq_tensor = torch.tensor([seq], dtype=torch.long)

            prompt_text = ""
            for turn in session["turns"]:
                if target in turn.get("files", []):
                    prompt_text = turn.get("prompt", "")
                    break
            prompt_feat = encode_prompt(prompt_text).unsqueeze(0)

            with torch.no_grad():
                logits = model(seq_tensor, prompt_feat)

            pred_top1 = logits.argmax(dim=1).item()
            pred_top5 = torch.topk(logits, k=min(5, logits.shape[1]), dim=1).indices[0].tolist()

            if pred_top1 == target_idx:
                top1_hits += 1
            if target_idx in pred_top5:
                top5_hits += 1
            total += 1

            if total >= max_turns:
                break
        if total >= max_turns:
            break

    if total == 0:
        print("  No test data available.")
        return 0

    print(f"\n  Test predictions: {total}")
    print(f"  Top-1 accuracy:  {top1_hits / total:.1%} ({top1_hits}/{total})")
    print(f"  Top-5 accuracy:  {top5_hits / total:.1%} ({top5_hits}/{total})")
    print()

    return top5_hits / total


# ============================================================================
# CLI
# ============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Neural file predictor")
    parser.add_argument("command", choices=["train", "benchmark", "info"],
                        help="Command to run")
    args = parser.parse_args()

    if args.command == "train":
        model, vocab, metrics = train_model()
        if model is not None:
            save_neural_model(model, vocab)
            benchmark_neural(model, vocab)

    elif args.command == "benchmark":
        model, vocab = load_neural_model()
        if model is None:
            print("No trained model found. Run 'train' first.")
            sys.exit(1)
        benchmark_neural(model, vocab)

    elif args.command == "info":
        model, vocab = load_neural_model()
        if model is None:
            print("No trained model found.")
        else:
            param_count = sum(p.numel() for p in model.parameters())
            print(f"Vocabulary: {len(vocab)} files")
            print(f"Parameters: {param_count:,}")
            print(f"Model file: {MODEL_PATH} ({MODEL_PATH.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
