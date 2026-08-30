# Status: production
# Path: imported by scripts/ modules
"""State — changelog management, structural diffing, state I/O."""
from lib.state.diff import structural_hash, diff_structural
from lib.state.changelog import load_changelog, save_changelog, append_changelog_entry, archive_old_entries
from lib.state.state_io import save_state, load_previous_state
