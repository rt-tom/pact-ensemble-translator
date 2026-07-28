import json
import os
import tempfile
from typing import Dict, Any

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

    def promote(self, status: str):
        """Promotes observations to main memory if status is 'complete'."""
        if status != 'complete':
            # Do nothing if quarantined or failed
            return

        obs = load_json(self.observations_path, {'glossary': {}, 'book_memory': {}})
        
        # Promote glossary
        glossary = load_json(self.glossary_path, {})
        self._merge_with_conflict_resolution(glossary, obs.get('glossary', {}))
        atomic_write(self.glossary_path, glossary)

        # Promote book_memory
        book_memory = load_json(self.book_memory_path, {})
        self._merge_with_conflict_resolution(book_memory, obs.get('book_memory', {}))
        atomic_write(self.book_memory_path, book_memory)
        
        # Clear observations after successful promotion
        atomic_write(self.observations_path, {'glossary': {}, 'book_memory': {}})

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
