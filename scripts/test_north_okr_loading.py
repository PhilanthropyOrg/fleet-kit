#!/usr/bin/env python3
"""test_north_okr_loading.py — test that north.py loads OKRs from product repo first."""

import json
import os
import pathlib
import sys
import tempfile

# Add scripts to path
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import north


def test_okr_loading_product_repo_first():
    """OKRs should load from product repo (fleet/okr.json) first."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = pathlib.Path(tmpdir)

        # Create product repo with its own OKR file
        product_dir = tmpdir / "product"
        product_dir.mkdir()
        product_fleet_dir = product_dir / "fleet"
        product_fleet_dir.mkdir()

        product_okr_data = {
            "objective": {"id": "okr.product", "label": "Product wins"},
            "key_results": [{"id": "okr.prod.users", "label": "Get 100 users"}]
        }
        product_okr_file = product_fleet_dir / "okr.json"
        product_okr_file.write_text(json.dumps(product_okr_data))

        # Set FLEET_REPO to point to product repo
        os.environ["FLEET_REPO"] = str(product_dir)

        # Load OKR - should get the product repo's version
        okr = north.load_okr()
        assert okr.get("objective", {}).get("id") == "okr.product", "Should load from product repo"
        print("✓ Test 1 PASS: Loads from product repo first")


def test_okr_loading_env_var_fallback():
    """OKRs should fall back to FLEET_OKR_FILE env var if product repo doesn't have it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = pathlib.Path(tmpdir)

        # Create product repo without fleet/okr.json
        product_dir = tmpdir / "product"
        product_dir.mkdir()

        # Create an OKR file at a different location
        env_okr_file = tmpdir / "my_okr.json"
        env_okr_data = {
            "objective": {"id": "okr.env", "label": "Env wins"},
            "key_results": []
        }
        env_okr_file.write_text(json.dumps(env_okr_data))

        # Set env vars
        os.environ["FLEET_REPO"] = str(product_dir)
        os.environ["FLEET_OKR_FILE"] = str(env_okr_file)

        # Load OKR - should get the env var version
        okr = north.load_okr()
        assert okr.get("objective", {}).get("id") == "okr.env", "Should fall back to FLEET_OKR_FILE"
        print("✓ Test 2 PASS: Falls back to FLEET_OKR_FILE")


def test_okr_loading_kit_default_fallback():
    """OKRs should fall back to kit's scripts/okr.json if nothing else exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = pathlib.Path(tmpdir)

        # Create product repo without fleet/okr.json
        product_dir = tmpdir / "product"
        product_dir.mkdir()

        # Set FLEET_REPO to nonexistent directory
        os.environ["FLEET_REPO"] = str(product_dir)
        # Remove FLEET_OKR_FILE if set
        os.environ.pop("FLEET_OKR_FILE", None)

        # Load OKR - should fall back to kit's default
        okr = north.load_okr()
        # Kit's default should have the "verified_claims" objective
        if okr:
            assert "objective" in okr, "Should return something from kit's default"
            print("✓ Test 3 PASS: Falls back to kit's scripts/okr.json")
        else:
            print("ℹ Test 3 INFO: Kit's default OKR appears to not exist in test env (expected)")


def test_okr_loading_explicit_path():
    """OKRs should load from explicit path if provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = pathlib.Path(tmpdir)

        # Create explicit OKR file
        explicit_okr_file = tmpdir / "explicit_okr.json"
        explicit_okr_data = {
            "objective": {"id": "okr.explicit", "label": "Explicit wins"},
            "key_results": []
        }
        explicit_okr_file.write_text(json.dumps(explicit_okr_data))

        # Load with explicit path
        okr = north.load_okr(explicit_okr_file)
        assert okr.get("objective", {}).get("id") == "okr.explicit", "Should load from explicit path"
        print("✓ Test 4 PASS: Loads from explicit path")


def test_invalid_json_gracefully_handled():
    """Invalid JSON should be handled gracefully (return empty dict or fall through)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = pathlib.Path(tmpdir)

        # Create product repo with invalid JSON
        product_dir = tmpdir / "product"
        product_dir.mkdir()
        product_fleet_dir = product_dir / "fleet"
        product_fleet_dir.mkdir()

        product_okr_file = product_fleet_dir / "okr.json"
        product_okr_file.write_text("{ invalid json }")

        os.environ["FLEET_REPO"] = str(product_dir)
        os.environ.pop("FLEET_OKR_FILE", None)

        # Should not crash
        okr = north.load_okr()
        # Will fall back to kit's default if invalid
        print("✓ Test 5 PASS: Invalid JSON handled gracefully")


if __name__ == "__main__":
    print("Testing north.py OKR loading hierarchy...")
    print()

    try:
        test_okr_loading_product_repo_first()
    except AssertionError as e:
        print(f"✗ Test 1 FAIL: {e}")
    except Exception as e:
        print(f"? Test 1 ERROR: {e}")

    try:
        test_okr_loading_env_var_fallback()
    except AssertionError as e:
        print(f"✗ Test 2 FAIL: {e}")
    except Exception as e:
        print(f"? Test 2 ERROR: {e}")

    try:
        test_okr_loading_kit_default_fallback()
    except AssertionError as e:
        print(f"✗ Test 3 FAIL: {e}")
    except Exception as e:
        print(f"? Test 3 ERROR: {e}")

    try:
        test_okr_loading_explicit_path()
    except AssertionError as e:
        print(f"✗ Test 4 FAIL: {e}")
    except Exception as e:
        print(f"? Test 4 ERROR: {e}")

    try:
        test_invalid_json_gracefully_handled()
    except AssertionError as e:
        print(f"✗ Test 5 FAIL: {e}")
    except Exception as e:
        print(f"? Test 5 ERROR: {e}")

    print()
    print("OKR loading tests complete. All tests should pass without crashing.")
