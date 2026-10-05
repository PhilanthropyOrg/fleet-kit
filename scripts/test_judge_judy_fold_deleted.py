"""A cleanup PR that deletes big files must still be reviewable (philanthropy gh#11182)."""

import importlib.util
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "members" / "judge-judy"
spec = importlib.util.spec_from_file_location(
    "fold_deleted_files", HERE / "fold_deleted_files.py"
)
fold_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fold_mod)

DELETED = (
    "diff --git a/data/big.csv b/data/big.csv\n"
    "deleted file mode 100644\n"
    "index 1111111..0000000\n"
    "--- a/data/big.csv\n"
    "+++ /dev/null\n"
    "@@ -1,3 +0,0 @@\n"
    "-a,b\n"
    "-1,2\n"
    "-3,4\n"
)
EDITED = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 2222222..3333333 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-old = 1\n"
    "+new = 2\n"
    " keep\n"
)


class FoldDeletedFiles(unittest.TestCase):
    def test_deleted_file_body_becomes_one_line_and_keeps_its_name(self):
        out = fold_mod.fold(DELETED + EDITED)
        self.assertIn("--- a/data/big.csv", out)
        self.assertIn("[whole file deleted: 3 lines]", out)
        self.assertNotIn("-a,b", out)

    def test_edited_file_is_untouched(self):
        out = fold_mod.fold(DELETED + EDITED)
        self.assertTrue(out.endswith(EDITED))
        self.assertEqual(fold_mod.fold(EDITED), EDITED)

    def test_reviewer_folds_before_it_cuts(self):
        sh = (HERE / "judge-judy.sh").read_text()
        self.assertLess(
            sh.index("fold_deleted_files.py"), sh.index('head -c "$MAX_DIFF_BYTES"')
        )


if __name__ == "__main__":
    unittest.main()
