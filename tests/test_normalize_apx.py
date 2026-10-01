import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")
PERL = shutil.which("perl")

# Inputs an APEXlang export could plausibly hold, plus the edges of the contract:
# LF only, one trailing newline, nothing else.
CASES = {
    "empty": b"",
    "lf-only": b"app ()\n",
    "no-final-newline": b"app ()",
    "many-final-newlines": b"app ()\n\n\n",
    "only-newlines": b"\n\n\n",
    "crlf": b"app ()\r\n  name: x\r\n",
    "lone-cr": b"app ()\r  name: x\r",
    "mixed": b"a\r\nb\rc\nd",
    "bom-lf": b"\xef\xbb\xbfapp ()\n",
    "bom-crlf": b"\xef\xbb\xbfa\r\nb\r\n\r\n",
    "bom-only": b"\xef\xbb\xbf",
    "non-ascii": "name: üé € \U0001f600\r\n".encode(),
}


@unittest.skipUnless(PWSH and PERL, "needs PowerShell Core and perl")
class NormalizeApxTests(unittest.TestCase):
    def test_both_normalizers_produce_identical_bytes(self) -> None:
        # An export or a post-import verification made in either shell must
        # match the files the other shell committed.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for shell in ("sh", "ps"):
                (root / shell).mkdir()
                for name, data in CASES.items():
                    (root / shell / f"{name}.apx").write_bytes(data)
            subprocess.run(["bash", str(ROOT / "scripts" / "normalize_apx.sh"), str(root / "sh")], check=True)
            subprocess.run(
                [PWSH, "-NoProfile", "-File", str(ROOT / "scripts" / "normalize_apx.ps1"), "-TargetDir", str(root / "ps")],
                check=True, capture_output=True,
            )
            for name in CASES:
                with self.subTest(case=name):
                    self.assertEqual(
                        (root / "ps" / f"{name}.apx").read_bytes(),
                        (root / "sh" / f"{name}.apx").read_bytes(),
                    )

    def test_normalized_output_is_lf_with_exactly_one_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, data in CASES.items():
                (root / f"{name}.apx").write_bytes(data)
            subprocess.run(["bash", str(ROOT / "scripts" / "normalize_apx.sh"), str(root)], check=True)
            for name, data in CASES.items():
                with self.subTest(case=name):
                    result = (root / f"{name}.apx").read_bytes()
                    self.assertNotIn(b"\r", result)
                    if data:  # an empty file stays empty
                        self.assertTrue(result.endswith(b"\n"))
                        self.assertFalse(result.endswith(b"\n\n"))


if __name__ == "__main__":
    unittest.main()
