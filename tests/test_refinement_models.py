import json
from unittest.mock import patch, MagicMock
from pathlib import Path
from alphred.config import Config, DEFAULT_CATALOG

def test_fetch_available_models_basic(tmp_path):
    # Setup tmp config
    cfg = Config(
        hermes_home=tmp_path / "hermes",
        alphred_home=tmp_path / "alphred",
        db_path=tmp_path / "alphred" / "t.db",
        queue_md_path=tmp_path / "alphred" / "QUEUE.MD",
        hermes_bin=None,
        api_base_url="http://localhost:8642/v1",
        gateway_url="http://localhost:8643",
        api_key=None
    )
    cfg.hermes_home.mkdir(parents=True, exist_ok=True)
    cfg.alphred_home.mkdir(parents=True, exist_ok=True)

    config_yaml = cfg.hermes_home / "config.yaml"
    config_yaml.write_text("model:\n  default: nvidia/llama-3.1-nemotron-ultra-253b-v1:free\n  provider: openrouter\n", encoding="utf-8")

    # Write a test model_catalog.json
    catalog_data = {
        "version": 1,
        "categories": {
            "coding": {
                "primary": {"model": "nvidia/llama-3.1-nemotron-ultra-253b-v1:free", "provider": "openrouter"}
            },
            "writing": {
                "primary": {"model": "nvidia/llama-3.1-nemotron-ultra-253b-v1:free", "provider": "openrouter"}
            }
        }
    }
    (cfg.alphred_home / "model_catalog.json").write_text(json.dumps(catalog_data), encoding="utf-8")

    # Mock python path and subprocess
    with patch.object(Config, "_venv_python", return_value=Path("/fake/python")), \
         patch("subprocess.run") as mock_run:
         
        # Mock subprocess stdout returning two providers
        mock_output = MagicMock()
        mock_output.returncode = 0
        mock_output.stdout = json.dumps({
            "openrouter": {
                "label": "OpenRouter",
                "models": ["nvidia/llama-3.1-nemotron-ultra-253b-v1:free", "google/gemma-2-27b-it"]
            },
            "nvidia": {
                "label": "NVIDIA NIM",
                "models": ["nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"]
            }
        })
        mock_run.return_value = mock_output

        # Mock reasoning cache file
        cache_file = cfg.hermes_home / "models_dev_cache.json"
        cache_data = {
            "nvidia": {
                "models": {
                    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": {"reasoning": True}
                }
            }
        }
        cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        res = cfg.fetch_available_models()
        
        assert res["current"] == "nvidia/llama-3.1-nemotron-ultra-253b-v1:free"
        assert res["current_provider"] == "openrouter"
        assert len(res["models"]) == 3
        
        # Check categories mapping for coding
        coding_model = [m for m in res["models"] if m["id"] == "nvidia/llama-3.1-nemotron-ultra-253b-v1:free"][0]
        assert "coding" in coding_model["categories"]
        assert "writing" in coding_model["categories"]
        assert coding_model["reasoning"] is False
        
        # Check reasoning model detection
        reasoning_model = [m for m in res["models"] if m["id"] == "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"][0]
        assert reasoning_model["reasoning"] is True
        assert reasoning_model["provider_label"] == "NVIDIA NIM"


def test_scout_update_free_only(tmp_path):
    from alphred.scout import run_scout_update
    from alphred.config import read_catalog_file

    alphred_home = tmp_path / "alphred"
    hermes_home = tmp_path / "hermes"
    alphred_home.mkdir(parents=True, exist_ok=True)
    hermes_home.mkdir(parents=True, exist_ok=True)

    config_yaml = hermes_home / "config.yaml"
    config_yaml.write_text("model:\n  default: base-model\n", encoding="utf-8")

    with patch("alphred.scout.fetch_openrouter_models", return_value=["nvidia/llama-3.1-nemotron-ultra-253b-v1:free"]), \
         patch("alphred.scout.fetch_nim_models", return_value=[]):
        success = run_scout_update(alphred_home, openrouter_key=None, nim_key=None, verbose=True, free_only=True)
        assert success is True

    catalog = read_catalog_file(alphred_home)
    assert catalog.get("free_only") is True
    
    # Coding primary should match free spec model
    coding_primary = catalog["categories"]["coding"]["primary"]["model"]
    assert coding_primary == "nvidia/llama-3.1-nemotron-ultra-253b-v1:free"


def test_scout_update_paid_only(tmp_path):
    from alphred.scout import run_scout_update
    from alphred.config import read_catalog_file

    alphred_home = tmp_path / "alphred"
    hermes_home = tmp_path / "hermes"
    alphred_home.mkdir(parents=True, exist_ok=True)
    hermes_home.mkdir(parents=True, exist_ok=True)

    config_yaml = hermes_home / "config.yaml"
    config_yaml.write_text("model:\n  default: base-model\n", encoding="utf-8")

    with patch("alphred.scout.fetch_openrouter_models", return_value=["anthropic/claude-3.5-sonnet"]), \
         patch("alphred.scout.fetch_nim_models", return_value=[]):
        success = run_scout_update(alphred_home, openrouter_key=None, nim_key=None, verbose=True, free_only=False)
        assert success is True

    catalog = read_catalog_file(alphred_home)
    assert catalog.get("free_only") is False

    # Coding primary should match paid spec model
    coding_primary = catalog["categories"]["coding"]["primary"]["model"]
    assert coding_primary == "anthropic/claude-3.5-sonnet"

