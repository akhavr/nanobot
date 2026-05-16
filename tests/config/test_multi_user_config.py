import json

from nanobot.config.loader import load_config, save_config
from nanobot.config.schema import Config


def test_multi_user_defaults_to_false() -> None:
    config = Config()

    assert config.multi_user is False


def test_config_parses_multi_user_from_camel_case() -> None:
    config = Config.model_validate({"multiUser": True})

    assert config.multi_user is True


def test_config_serialises_multi_user_as_camel_case(tmp_path) -> None:
    config = Config(multi_user=True)
    config_path = tmp_path / "config.json"

    save_config(config, config_path)

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["multiUser"] is True


def test_load_config_parses_multi_user_from_json(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"multiUser": True}), encoding="utf-8")

    config = load_config(config_path)

    assert config.multi_user is True
