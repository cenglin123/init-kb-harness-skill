"""Structural checks for SKILL.md and its on-demand refs/ documentation."""

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "SKILL.md"
REFS_DIR = REPO_ROOT / "refs"
SCRIPTS_DIR = REPO_ROOT / "scripts"
MAX_SKILL_BYTES = 18 * 1024
MAX_SKILL_LINES = 160


class TestSkillStructure(unittest.TestCase):
    def test_skill_md_size_and_line_budget(self):
        self.assertTrue(SKILL_MD.is_file(), "SKILL.md must exist")
        size = SKILL_MD.stat().st_size
        self.assertLess(
            size,
            MAX_SKILL_BYTES,
            f"SKILL.md is {size} bytes, exceeding {MAX_SKILL_BYTES} byte budget",
        )
        lines = SKILL_MD.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(
            len(lines),
            MAX_SKILL_LINES,
            f"SKILL.md has {len(lines)} lines, exceeding {MAX_SKILL_LINES} line budget",
        )
        self.assertGreaterEqual(len(lines), 40, "SKILL.md appears unexpectedly truncated")
        self.assertEqual(lines[0], "---", "SKILL.md must preserve YAML frontmatter")

    def test_all_refs_referenced_in_skill_md_exist(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        # Match concrete refs/<filename> paths (exclude wildcard patterns like refs/*.md)
        refs = sorted(set(re.findall(r"refs/[A-Za-z0-9._-]+", text)))
        self.assertGreater(len(refs), 0, "SKILL.md should reference refs/ documents")
        missing = [ref for ref in refs if not (REPO_ROOT / ref).is_file()]
        self.assertEqual(
            missing,
            [],
            f"SKILL.md references missing refs/ files: {missing}",
        )

    def test_bootstrap_phases_doc_exists_and_covers_core_sections(self):
        phases_doc = REFS_DIR / "bootstrap-phases.md"
        self.assertTrue(phases_doc.is_file(), "refs/bootstrap-phases.md must exist")
        text = phases_doc.read_text(encoding="utf-8")
        required_markers = [
            "Phase 0",
            "Phase 1",
            "Phase 1.5",
            "Phase 2",
            "Phase 3",
            "Bootstrap 完成判定",
            "幂等性",
            "bootstrap_phase",
            "旧版兼容",
            "产物清单",
        ]
        for marker in required_markers:
            self.assertIn(
                marker,
                text,
                f"refs/bootstrap-phases.md missing required section/marker: {marker}",
            )

    def test_script_inventory_exists_and_covers_all_scripts(self):
        inventory_doc = REFS_DIR / "script-inventory.md"
        self.assertTrue(inventory_doc.is_file(), "refs/script-inventory.md must exist")
        text = inventory_doc.read_text(encoding="utf-8")
        script_files = sorted(p.name for p in SCRIPTS_DIR.glob("*.py"))
        self.assertGreater(len(script_files), 0, "scripts/ must contain Python scripts")
        missing = [name for name in script_files if name not in text]
        self.assertEqual(
            missing,
            [],
            f"refs/script-inventory.md is missing scripts: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
