"""Tests for catalog transport batching, compression, and framed decoding."""

from __future__ import annotations

import base64
import gzip
import json
import subprocess
import sys
import unittest
import zlib
from unittest.mock import patch
from pathlib import Path

from scripts.db_targets import Target
from scripts.schema_catalog import (
    CatalogError,
    MAX_CATALOG_BYTES,
    ObjectKey,
    _inventory_signature,
    decode_catalog_payload,
    parse_inventory,
    parse_snapshot,
)
import _no_real_sqlcl  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "schema_catalog"


def target() -> Target:
    return Target("dev", "dev-profile", "APP_DEV", "APP_DEV", "development")


def frame_legacy(payload: dict, phase: str | None = None) -> str:
    name = phase or payload["phase"]
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        f"CATALOG_PAYLOAD_BEGIN:{name}\n"
        f"{body}\n"
        f"CATALOG_PAYLOAD_END:{name}\n"
        f"CATALOG_VERIFIED:{name}\n"
    )


def frame_compressed(payload: dict, phase: str | None = None, chunk_raw_bytes: int = 12000) -> str:
    name = phase or payload["phase"]
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(body)
    b64 = base64.b64encode(compressed).decode("ascii")
    chunk_b64_len = (chunk_raw_bytes // 3) * 4
    chunks = [b64[i : i + chunk_b64_len] for i in range(0, len(b64), chunk_b64_len)]
    lines = [
        f"CATALOG_PAYLOAD_BEGIN:{name}",
        "CATALOG_ENCODING:gzip-base64-v1",
        *chunks,
        f"CATALOG_PAYLOAD_END:{name}",
        f"CATALOG_VERIFIED:{name}",
        "",
    ]
    return "\n".join(lines)


class CatalogTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = json.loads((FIXTURES / "transport-cases.json").read_text(encoding="utf-8"))
        self.base_inventory = json.loads((FIXTURES / "owner-inventory.json").read_text(encoding="utf-8"))

    def test_decode_catalog_payload_supports_legacy_and_compressed_equality(self) -> None:
        payload = dict(self.base_inventory)
        legacy_lines = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).splitlines()
        decoded_legacy = decode_catalog_payload(legacy_lines)
        self.assertEqual(decoded_legacy, payload)

        compressed_output = frame_compressed(payload)
        lines = [line for line in compressed_output.splitlines() if line.startswith("CATALOG_ENCODING:") or (not line.startswith("CATALOG_") and line)]
        decoded_compressed = decode_catalog_payload(lines)
        self.assertEqual(decoded_compressed, payload)

        inv_legacy = parse_inventory(frame_legacy(payload), target())
        inv_comp = parse_inventory(compressed_output, target())
        self.assertEqual(inv_legacy.objects, inv_comp.objects)
        self.assertEqual(inv_legacy.identity, inv_comp.identity)
        self.assertEqual(_inventory_signature(inv_legacy.objects), _inventory_signature(inv_comp.objects))

    def test_native_sqlcl_trailing_blank_before_end_marker_is_presentation(self) -> None:
        framed = frame_compressed(self.base_inventory).replace(
            "CATALOG_PAYLOAD_END:inventory", "\nCATALOG_PAYLOAD_END:inventory")
        observed = parse_inventory(framed, target())
        self.assertEqual(observed.objects, parse_inventory(frame_legacy(self.base_inventory), target()).objects)

    def test_large_partitioned_inventory_with_over_10000_rows_and_unicode_roundtrips(self) -> None:
        # Build >= 10,000 inventory rows with partition subobjects and identity sequences
        payload = dict(self.base_inventory)
        objects = []

        # Table with identity sequence
        objects.append({
            "owner": "APP_DEV",
            "name": "CUSTOMERS",
            "type": "TABLE",
            "status": "VALID",
            "last_ddl_time": "2026-09-27T12:00:00Z",
        })
        objects.append({
            "owner": "APP_DEV",
            "name": "ISEQ$$_CUSTOMERS",
            "type": "SEQUENCE",
            "status": "VALID",
            "last_ddl_time": "2026-09-27T12:00:00Z",
            "identity_sequence": True,
            "identity_table_name": "CUSTOMERS",
        })

        # Add partitioned objects to exceed 10,000 rows
        for i in range(1, 10005):
            table_idx = (i // 1000)
            part_name = f"P_{i % 1000:04d}"
            objects.append({
                "owner": "APP_DEV",
                "name": f"PART_TAB_{table_idx}",
                "type": "TABLE PARTITION",
                "subobject_name": part_name,
                "status": "VALID",
                "object_id": 10000 + i,
                "data_object_id": 20000 + i,
                "object_timestamp": "2026-09-27 12:00:00",
                "last_ddl_time": "2026-09-27T12:00:00Z",
                "identity_sequence": False,
            })

        self.assertGreaterEqual(len(objects), 10000)
        payload["objects"] = objects

        inv_legacy = parse_inventory(frame_legacy(payload), target())
        inv_comp = parse_inventory(frame_compressed(payload), target())

        self.assertEqual(len(inv_comp.objects), len(objects))
        self.assertEqual(inv_legacy.objects, inv_comp.objects)
        self.assertEqual(_inventory_signature(inv_legacy.objects), _inventory_signature(inv_comp.objects))

        # Check identity sequence mapping preserved
        seq_key = ObjectKey("APP_DEV", "ISEQ$$_CUSTOMERS", "SEQUENCE")
        self.assertIn(seq_key, inv_comp.objects)
        self.assertTrue(inv_comp.objects[seq_key]["identity_sequence"])
        self.assertEqual(inv_comp.objects[seq_key]["identity_table_name"], "CUSTOMERS")

    def test_over_1_mib_multilingual_unicode_snapshot_roundtrip(self) -> None:
        # Build > 1 MiB snapshot with supplementary emojis, combining accents, math, and multi-byte text
        snapshot_payload = json.loads((FIXTURES / "owner-snapshot.json").read_text(encoding="utf-8"))
        multilingual_base = self.cases["multilingual_sample"]
        # Repeat to create > 1.2 MiB of UTF-8 DDL
        large_ddl = "/* " + (multilingual_base * 25000) + " */\nCREATE TABLE CUSTOMERS(ID NUMBER);"
        self.assertGreater(len(large_ddl.encode("utf-8")), 1024 * 1024)

        snapshot_payload["definitions"][0]["raw_ddl"] = large_ddl

        snap_legacy = parse_snapshot(frame_legacy(snapshot_payload), target(), (("CUSTOMERS", "TABLE"),))
        snap_comp = parse_snapshot(frame_compressed(snapshot_payload), target(), (("CUSTOMERS", "TABLE"),))

        def_legacy = snap_legacy.objects[ObjectKey("APP_DEV", "CUSTOMERS", "TABLE")]
        def_comp = snap_comp.objects[ObjectKey("APP_DEV", "CUSTOMERS", "TABLE")]

        self.assertEqual(def_legacy.raw_ddl, def_comp.raw_ddl)
        self.assertEqual(len(def_comp.raw_ddl.encode("utf-8")), len(large_ddl.encode("utf-8")))
        self.assertIn("😀", def_comp.raw_ddl)
        self.assertIn("é", def_comp.raw_ddl)
        self.assertIn("∑∏√", def_comp.raw_ddl)

    def test_unsupported_encoding_refuses(self) -> None:
        lines = [
            "CATALOG_ENCODING:zstd-base64-v1",
            "YWJj",
        ]
        with self.assertRaises(CatalogError) as ctx:
            decode_catalog_payload(lines)
        self.assertIn("unsupported catalog encoding", str(ctx.exception))

        frame = (
            "CATALOG_PAYLOAD_BEGIN:inventory\n"
            "CATALOG_ENCODING:unsupported-v1\n"
            "YWJj\n"
            "CATALOG_PAYLOAD_END:inventory\n"
            "CATALOG_VERIFIED:inventory\n"
        )
        with self.assertRaises(CatalogError):
            parse_inventory(frame, target())

    def test_invalid_base64_refuses(self) -> None:
        # Bad alphabet
        with self.assertRaises(CatalogError):
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", "%%%INVALID%%%"])

        # Interior whitespace
        with self.assertRaises(CatalogError):
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", "YWJj ZGVm"])

        # Trailing whitespace
        with self.assertRaises(CatalogError):
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", "YWJj "])

        # Bad padding
        with self.assertRaises(CatalogError):
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", "YW==="])

        # Padding in middle
        with self.assertRaises(CatalogError):
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", "YW==YWJj"])

    def test_gzip_header_corruption_refuses(self) -> None:
        # Not starting with 1f 8b
        corrupt_header = base64.b64encode(b"\x00\x00\x08\x00\x00\x00\x00\x00\x00\x00" + b"payload").decode("ascii")
        with self.assertRaises(CatalogError) as ctx:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", corrupt_header])
        self.assertIn("gzip", str(ctx.exception).lower())

    def test_gzip_trailer_and_crc_corruption_refuses(self) -> None:
        raw = json.dumps(self.base_inventory).encode("utf-8")
        compressed = bytearray(gzip.compress(raw))

        # Corrupt CRC (4 bytes from end: bytes -8 to -5)
        corrupt_crc = bytearray(compressed)
        corrupt_crc[-8] ^= 0xFF
        b64_crc = base64.b64encode(corrupt_crc).decode("ascii")
        with self.assertRaises(CatalogError) as ctx:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64_crc])
        self.assertIn("decompression failed", str(ctx.exception))

        # Corrupt ISIZE (last 4 bytes: bytes -4 to 0)
        corrupt_isize = bytearray(compressed)
        corrupt_isize[-1] ^= 0xFF
        b64_isize = base64.b64encode(corrupt_isize).decode("ascii")
        with self.assertRaises(CatalogError) as ctx:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64_isize])
        self.assertIn("decompression failed", str(ctx.exception))

    def test_gzip_truncation_refuses(self) -> None:
        raw = json.dumps(self.base_inventory).encode("utf-8")
        compressed = gzip.compress(raw)

        # Truncate halfway
        truncated = compressed[: len(compressed) // 2]
        b64_trunc = base64.b64encode(truncated).decode("ascii")
        with self.assertRaises(CatalogError) as ctx:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64_trunc])
        self.assertTrue("truncated" in str(ctx.exception) or "decompression failed" in str(ctx.exception))

        # Truncate trailer
        truncated_trailer = compressed[:-4]
        b64_tt = base64.b64encode(truncated_trailer).decode("ascii")
        with self.assertRaises(CatalogError) as ctx:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64_tt])
        self.assertTrue("truncated" in str(ctx.exception) or "decompression failed" in str(ctx.exception))

    def test_trailing_junk_and_concatenated_multistream_refuse(self) -> None:
        raw = json.dumps(self.base_inventory).encode("utf-8")
        compressed = gzip.compress(raw)

        # Trailing junk bytes
        junk = compressed + b"EXTRA_TRAILING_JUNK"
        b64_junk = base64.b64encode(junk).decode("ascii")
        with self.assertRaises(CatalogError) as ctx:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64_junk])
        self.assertIn("trailing data", str(ctx.exception))

        # Concatenated second valid gzip stream
        concat = compressed + compressed
        b64_concat = base64.b64encode(concat).decode("ascii")
        with self.assertRaises(CatalogError) as ctx:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64_concat])
        self.assertIn("multiple streams", str(ctx.exception))

    def test_chunk_boundary_trailing_data_refuses_without_stalling(self) -> None:
        child = """import sys
from scripts.schema_catalog import decode_catalog_payload, CatalogError
try:
 decode_catalog_payload(['CATALOG_ENCODING:gzip-base64-v1', sys.stdin.read()])
except CatalogError as e:
 if 'trailing data' in str(e): sys.exit(0)
 raise
sys.exit(1)
"""
        for length in (65536, 131072):
            payload = {"schemaVersion": 1, "phase": "inventory", "padding": ""}
            empty = json.dumps(payload, separators=(",", ":")).encode()
            payload["padding"] = "x" * (length - len(empty))
            compressed = gzip.compress(json.dumps(payload, separators=(",", ":")).encode())
            for suffix in (b"junk", gzip.compress(b"{}")):
                with self.subTest(length=length, suffix=suffix[:4]):
                    result = subprocess.run([sys.executable, "-c", child], cwd=ROOT,
                                            input=base64.b64encode(compressed + suffix).decode(),
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_truncated_stream_never_uses_unbounded_flush(self) -> None:
        actual = zlib.decompressobj(31)

        class BoundedDecoder:
            def __getattr__(self, name):
                return getattr(actual, name)

            def flush(self, *args):
                raise AssertionError("flush length is only an initial allocation, not an output cap")

        payload = base64.b64encode(gzip.compress(json.dumps(self.base_inventory).encode())[:-4]).decode()
        with patch("scripts.schema_catalog.zlib.decompressobj", return_value=BoundedDecoder()):
            with self.assertRaisesRegex(CatalogError, "truncated"):
                decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", payload])

    def test_invalid_utf8_refuses(self) -> None:
        # Valid gzip of invalid UTF-8 bytes
        invalid_utf8_bytes = b"{\"text\": \"" + b"\xff\xfe\xfd" + b"\"}"
        compressed = gzip.compress(invalid_utf8_bytes)
        b64 = base64.b64encode(compressed).decode("ascii")
        with self.assertRaises(CatalogError) as ctx:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64])
        self.assertIn("invalid utf-8", str(ctx.exception).lower())

    def test_decoded_oversize_refuses_during_streaming_decompression(self) -> None:
        self.assertEqual(MAX_CATALOG_BYTES, 128 * 1024 * 1024)
        # 130 MiB of zeros compresses to ~130 KiB
        co = zlib.compressobj(level=9, wbits=31)
        compressed = co.compress(b"0" * (130 * 1024 * 1024)) + co.flush()
        b64 = base64.b64encode(compressed).decode("ascii")
        with self.assertRaises(CatalogError) as ctx:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64])
        self.assertIn("maximum permitted size", str(ctx.exception))

    def test_missing_end_marker_or_sentinel_refuses(self) -> None:
        output_missing_end = (
            "CATALOG_PAYLOAD_BEGIN:inventory\n"
            "CATALOG_ENCODING:gzip-base64-v1\n"
            + base64.b64encode(gzip.compress(json.dumps(self.base_inventory).encode("utf-8"))).decode("ascii") + "\n"
            "CATALOG_VERIFIED:inventory\n"
        )
        with self.assertRaises(CatalogError):
            parse_inventory(output_missing_end, target())

        output_missing_verified = (
            "CATALOG_PAYLOAD_BEGIN:inventory\n"
            "CATALOG_ENCODING:gzip-base64-v1\n"
            + base64.b64encode(gzip.compress(json.dumps(self.base_inventory).encode("utf-8"))).decode("ascii") + "\n"
            "CATALOG_PAYLOAD_END:inventory\n"
        )
        with self.assertRaises(CatalogError):
            parse_inventory(output_missing_verified, target())

    def test_duplicate_real_object_keys_refuse(self) -> None:
        payload = dict(self.base_inventory)
        dup = {"owner": "APP_DEV", "name": "CUSTOMERS", "type": "TABLE", "status": "VALID", "last_ddl_time": "2026-09-27T12:00:00Z"}
        payload["objects"] = [dup, dict(dup)]

        with self.assertRaises(CatalogError) as ctx:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", base64.b64encode(gzip.compress(json.dumps(payload).encode("utf-8"))).decode("ascii")])
        self.assertIn("repeats", str(ctx.exception))

    def test_invalid_schema_version_and_phase_refuse(self) -> None:
        payload_bad_version = dict(self.base_inventory)
        payload_bad_version["schemaVersion"] = 2
        with self.assertRaises(CatalogError) as ctx:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", base64.b64encode(gzip.compress(json.dumps(payload_bad_version).encode("utf-8"))).decode("ascii")])
        self.assertIn("schema version", str(ctx.exception))

        payload_bad_phase = dict(self.base_inventory)
        payload_bad_phase["phase"] = "unknown_phase"
        with self.assertRaises(CatalogError) as ctx:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", base64.b64encode(gzip.compress(json.dumps(payload_bad_phase).encode("utf-8"))).decode("ascii")])
        self.assertIn("phase", str(ctx.exception))

    def test_conformance_cases_from_fixture(self) -> None:
        for case in self.cases["conformance_cases"]:
            with self.subTest(case_id=case["id"]):
                if "lines" in case:
                    lines = case["lines"]
                elif case["id"] == "invalid_schema_version":
                    bad = dict(self.base_inventory)
                    bad["schemaVersion"] = 99
                    lines = ["CATALOG_ENCODING:gzip-base64-v1", base64.b64encode(gzip.compress(json.dumps(bad).encode("utf-8"))).decode("ascii")]
                elif case["id"] == "invalid_phase":
                    bad = dict(self.base_inventory)
                    bad["phase"] = "bogus"
                    lines = ["CATALOG_ENCODING:gzip-base64-v1", base64.b64encode(gzip.compress(json.dumps(bad).encode("utf-8"))).decode("ascii")]
                elif case["id"] == "duplicate_real_keys":
                    bad = dict(self.base_inventory)
                    dup = {"owner": "APP_DEV", "name": "CUSTOMERS", "type": "TABLE", "status": "VALID", "last_ddl_time": "2026-09-27T12:00:00Z"}
                    bad["objects"] = [dup, dict(dup)]
                    lines = ["CATALOG_ENCODING:gzip-base64-v1", base64.b64encode(gzip.compress(json.dumps(bad).encode("utf-8"))).decode("ascii")]
                else:
                    continue

                with self.assertRaises(CatalogError) as ctx:
                    decode_catalog_payload(lines)
                self.assertIn(case["expected_error"].lower(), str(ctx.exception).lower())


    def test_strict_json_rejects_nan_infinity_legacy_and_compressed(self) -> None:
        for bad_constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(bad_constant=bad_constant):
                # Build raw json string with the non-standard constant
                raw_json = (
                    '{"schemaVersion": 1, "phase": "inventory", "complete": true, '
                    f'"coverage": {{"ownerComplete": true, "catalogs": ["ALL_OBJECTS"], "path": "OWNER_SESSION"}}, '
                    f'"identity": {{"session_user": "APP_DEV", "current_schema": "APP_DEV", "db_name": "XE", '
                    '"db_unique_name": "XE", "service_name": "XE", "container_id": "0", "container_name": "NON-CDB", '
                    f'"edition": "NONE", "database_version": "23.0"}}, "objects": [], '
                    f'"started_at": "2026-09-27T12:00:00Z", "completed_at": "2026-09-27T12:00:01Z", "val": {bad_constant}}}'
                )

                # 1. Legacy path
                with self.assertRaises(CatalogError) as ctx_legacy:
                    decode_catalog_payload(raw_json.splitlines())
                self.assertIn("non-standard", str(ctx_legacy.exception).lower())

                # 2. Compressed path
                compressed = gzip.compress(raw_json.encode("utf-8"))
                b64 = base64.b64encode(compressed).decode("ascii")
                with self.assertRaises(CatalogError) as ctx_comp:
                    decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64])
                self.assertIn("non-standard", str(ctx_comp.exception).lower())

    def test_strict_json_rejects_duplicate_member_keys_at_any_depth(self) -> None:
        # Duplicate key at root level
        raw_root_dup = (
            '{"schemaVersion": 1, "phase": "inventory", "phase": "inventory", "complete": true, '
            '"coverage": {"ownerComplete": true, "catalogs": ["ALL_OBJECTS"], "path": "OWNER_SESSION"}, '
            '"identity": {"session_user": "APP_DEV", "current_schema": "APP_DEV", "db_name": "XE", '
            '"db_unique_name": "XE", "service_name": "XE", "container_id": "0", "container_name": "NON-CDB", '
            '"edition": "NONE", "database_version": "23.0"}, "objects": [], '
            '"started_at": "2026-09-27T12:00:00Z", "completed_at": "2026-09-27T12:00:01Z"}'
        )
        with self.assertRaises(CatalogError) as ctx_root_legacy:
            decode_catalog_payload(raw_root_dup.splitlines())
        self.assertIn("duplicate", str(ctx_root_legacy.exception).lower())

        b64_root = base64.b64encode(gzip.compress(raw_root_dup.encode("utf-8"))).decode("ascii")
        with self.assertRaises(CatalogError) as ctx_root_comp:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64_root])
        self.assertIn("duplicate", str(ctx_root_comp.exception).lower())

        # Duplicate key at nested level (inside identity)
        raw_nested_dup = (
            '{"schemaVersion": 1, "phase": "inventory", "complete": true, '
            '"coverage": {"ownerComplete": true, "catalogs": ["ALL_OBJECTS"], "path": "OWNER_SESSION"}, '
            '"identity": {"session_user": "APP_DEV", "session_user": "APP_DEV", "current_schema": "APP_DEV", "db_name": "XE", '
            '"db_unique_name": "XE", "service_name": "XE", "container_id": "0", "container_name": "NON-CDB", '
            '"edition": "NONE", "database_version": "23.0"}, "objects": [], '
            '"started_at": "2026-09-27T12:00:00Z", "completed_at": "2026-09-27T12:00:01Z"}'
        )
        with self.assertRaises(CatalogError) as ctx_nested_legacy:
            decode_catalog_payload(raw_nested_dup.splitlines())
        self.assertIn("duplicate", str(ctx_nested_legacy.exception).lower())

        b64_nested = base64.b64encode(gzip.compress(raw_nested_dup.encode("utf-8"))).decode("ascii")
        with self.assertRaises(CatalogError) as ctx_nested_comp:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64_nested])
        self.assertIn("duplicate", str(ctx_nested_comp.exception).lower())

    def test_schema_version_boolean_true_refuses_legacy_and_compressed(self) -> None:
        raw_bool_version = (
            '{"schemaVersion": true, "phase": "inventory", "complete": true, '
            '"coverage": {"ownerComplete": true, "catalogs": ["ALL_OBJECTS"], "path": "OWNER_SESSION"}, '
            '"identity": {"session_user": "APP_DEV", "current_schema": "APP_DEV", "db_name": "XE", '
            '"db_unique_name": "XE", "service_name": "XE", "container_id": "0", "container_name": "NON-CDB", '
            '"edition": "NONE", "database_version": "23.0"}, "objects": [], '
            '"started_at": "2026-09-27T12:00:00Z", "completed_at": "2026-09-27T12:00:01Z"}'
        )
        # 1. Legacy
        with self.assertRaises(CatalogError) as ctx_legacy:
            decode_catalog_payload(raw_bool_version.splitlines())
        self.assertIn("schema version", str(ctx_legacy.exception).lower())

        # 2. Compressed
        b64 = base64.b64encode(gzip.compress(raw_bool_version.encode("utf-8"))).decode("ascii")
        with self.assertRaises(CatalogError) as ctx_comp:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64])
        self.assertIn("schema version", str(ctx_comp.exception).lower())

        # Also test bool false, string "1", float 1.0
        for bad_val in ("false", '"1"', "1.0"):
            with self.subTest(bad_val=bad_val):
                bad_raw = raw_bool_version.replace('"schemaVersion": true', f'"schemaVersion": {bad_val}')
                with self.assertRaises(CatalogError):
                    decode_catalog_payload(bad_raw.splitlines())

    def test_object_key_types_validated_before_hashing(self) -> None:
        # Validate that malicious or malformed subobject_name types (list, dict, int)
        # raise CatalogError rather than unhandled TypeError: unhashable type traceback
        bad_subobjects = [
            ["evil", "list"],
            {"nested": "dict"},
            12345,
            True,
        ]
        for bad_sub in bad_subobjects:
            with self.subTest(bad_sub=bad_sub):
                payload = dict(self.base_inventory)
                row = {
                    "owner": "APP_DEV",
                    "name": "CUSTOMERS",
                    "type": "TABLE",
                    "subobject_name": bad_sub,
                    "status": "VALID",
                    "last_ddl_time": "2026-09-27T12:00:00Z",
                }
                payload["objects"] = [row]
                b64 = base64.b64encode(gzip.compress(json.dumps(payload).encode("utf-8"))).decode("ascii")

                # Must raise CatalogError, NOT TypeError
                with self.assertRaises(CatalogError) as ctx:
                    decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64])
                self.assertIn("malformed subobject name", str(ctx.exception).lower())

        # Also test malformed owner / name / type (e.g. list or int)
        bad_keys = [
            {"owner": ["APP_DEV"], "name": "CUSTOMERS", "type": "TABLE"},
            {"owner": "APP_DEV", "name": {"name": "CUSTOMERS"}, "type": "TABLE"},
            {"owner": "APP_DEV", "name": "CUSTOMERS", "type": 999},
        ]
        for bad_row in bad_keys:
            with self.subTest(bad_row=bad_row):
                payload = dict(self.base_inventory)
                payload["objects"] = [bad_row]
                b64 = base64.b64encode(gzip.compress(json.dumps(payload).encode("utf-8"))).decode("ascii")
                with self.assertRaises(CatalogError) as ctx:
                    decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64])
                self.assertIn("incomplete object key", str(ctx.exception).lower())

    def test_gzip_audit_guards_truncation_and_multistream(self) -> None:
        payload = dict(self.base_inventory)
        valid_gz = gzip.compress(json.dumps(payload).encode("utf-8"))

        # Test truncation at multiple offsets
        for cut in range(1, 15):
            truncated = valid_gz[:-cut]
            b64_trunc = base64.b64encode(truncated).decode("ascii")
            with self.subTest(trunc_cut=cut):
                with self.assertRaises(CatalogError) as ctx:
                    decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64_trunc])
                self.assertTrue(
                    "truncated" in str(ctx.exception).lower() or "gzip decompression failed" in str(ctx.exception).lower()
                )

        # Test trailing data (1 byte to 20 bytes)
        for trailing_len in (1, 4, 16):
            with self.subTest(trailing_len=trailing_len):
                corrupt = valid_gz + b"X" * trailing_len
                b64_corrupt = base64.b64encode(corrupt).decode("ascii")
                with self.assertRaises(CatalogError) as ctx:
                    decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64_corrupt])
                self.assertIn("trailing data or multiple streams", str(ctx.exception).lower())

        # Test concatenated multistream gzip
        concat_gz = valid_gz + valid_gz
        b64_concat = base64.b64encode(concat_gz).decode("ascii")
        with self.assertRaises(CatalogError) as ctx:
            decode_catalog_payload(["CATALOG_ENCODING:gzip-base64-v1", b64_concat])
        self.assertIn("trailing data or multiple streams", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
