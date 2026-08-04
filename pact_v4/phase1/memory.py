import json
import os
import tempfile
from typing import Any, Dict, Optional

def atomic_write(filepath: str, data: Any):
    dir_name = os.path.dirname(filepath)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, text=True)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, filepath)

def load_json(filepath: str, default: Any = None) -> Any:
    if not os.path.exists(filepath):
        return default if default is not None else {}
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)

class MemoryManager:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.glossary_path = os.path.join(base_dir, 'glossary.json')
        self.book_memory_path = os.path.join(base_dir, 'book_memory.json')
        self.chapter_memory_path = os.path.join(base_dir, 'chapter_memory.json')
        self.observations_path = os.path.join(base_dir, 'observations.json')

    def create_snapshot(self):
        """Creates a frozen chapter_memory.json from current glossary and book_memory."""
        glossary = load_json(self.glossary_path)
        book_memory = load_json(self.book_memory_path)
        snapshot = {
            'glossary': glossary,
            'book_memory': book_memory
        }
        atomic_write(self.chapter_memory_path, snapshot)
        # Clear observations for the new chapter
        atomic_write(self.observations_path, {'glossary': {}, 'book_memory': {}})

    def add_observation(self, category: str, key: str, value: Any):
        """Adds an observation to the shadow memory."""
        obs = load_json(self.observations_path, {'glossary': {}, 'book_memory': {}})
        if category not in obs:
            obs[category] = {}
        obs[category][key] = value
        atomic_write(self.observations_path, obs)

    def promote(self, status: str, *, quarantined_chunks: Optional[set] = None):
        """Promote observations to main memory based on terminal status.

        ``complete``: promote all observations (existing behaviour).
        ``accepted_degraded``: promote observations only from non-quarantined
        chunks (owner decision 2026-08-04, B7). ``quarantined_chunks`` is the
        set of chunk_ids that were quarantined; observations keyed by those
        chunk_ids are excluded.
        ``failed`` / ``quarantined``: do nothing (existing behaviour).
        """
        if status not in ('complete', 'accepted_degraded'):
            return

        obs = load_json(self.observations_path, {'glossary': {}, 'book_memory': {}})

        if status == 'accepted_degraded' and quarantined_chunks:
            obs = self._filter_quarantined_obs(obs, quarantined_chunks)

        glossary = load_json(self.glossary_path, {})
        self._merge_with_conflict_resolution(glossary, obs.get('glossary', {}))
        atomic_write(self.glossary_path, glossary)

        book_memory = load_json(self.book_memory_path, {})
        self._merge_with_conflict_resolution(book_memory, obs.get('book_memory', {}))
        atomic_write(self.book_memory_path, book_memory)

        atomic_write(self.observations_path, {'glossary': {}, 'book_memory': {}})

    def _filter_quarantined_obs(
        self, obs: Dict, quarantined_chunks: set
    ) -> Dict:
        """Remove observations attributed to quarantined chunks.

        Observations may carry a ``chunk_id`` field; those matching a
        quarantined chunk are dropped. Observations without a ``chunk_id``
        are kept (they are chapter-level, not chunk-specific).
        """
        filtered: Dict[str, Any] = {}
        for category in ('glossary', 'book_memory'):
            entries = obs.get(category, {})
            if not isinstance(entries, dict):
                continue
            kept: Dict[str, Any] = {}
            for key, value in entries.items():
                if isinstance(value, dict) and value.get('chunk_id') in quarantined_chunks:
                    continue
                kept[key] = value
            filtered[category] = kept
        return filtered

    def _merge_with_conflict_resolution(self, main_mem: Dict, new_obs: Dict):
        for key, value in new_obs.items():
            if key in main_mem:
                existing = main_mem[key]
                if isinstance(existing, dict) and existing.get('status') in ('established', 'locked'):
                    # Conflict: do not overwrite
                    continue
            main_mem[key] = value

    def rollback(self):
        """Rollbacks to the last snapshot."""
        snapshot = load_json(self.chapter_memory_path, None)
        if snapshot:
            atomic_write(self.glossary_path, snapshot.get('glossary', {}))
            atomic_write(self.book_memory_path, snapshot.get('book_memory', {}))
            # Clear observations on rollback
            atomic_write(self.observations_path, {'glossary': {}, 'book_memory': {}})
