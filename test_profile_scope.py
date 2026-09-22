from unittest.mock import patch

import adapter


def test_requirements_use_the_active_profile_scope():
    def scoped_secret(name, default=""):
        return "test.meshcore.invalid" if name == "MESHCORE_HOST" else default

    with patch.dict("os.environ", {}, clear=False), patch.object(
        adapter, "_get_scoped_secret", side_effect=scoped_secret
    ):
        assert adapter.check_requirements() is True


def test_env_enablement_reads_scoped_profile_values():
    values = {
        "MESHCORE_HOST": "test.meshcore.invalid",
        "MESHCORE_PORT": "5001",
        "MESHCORE_ENABLE_DMS": "true",
    }

    with patch.object(
        adapter,
        "_get_scoped_secret",
        side_effect=lambda name, default="": values.get(name, default),
    ):
        enabled = adapter._env_enablement()

    assert enabled is not None
    assert enabled["host"] == "test.meshcore.invalid"
    assert enabled["port"] == 5001
    assert enabled["enable_dms"] == "true"
