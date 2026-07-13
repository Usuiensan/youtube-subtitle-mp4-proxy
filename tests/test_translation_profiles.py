from app.translation_profiles import profile_labels, profile_models


def test_profile_models_preserve_defaults_and_aliases() -> None:
    values = {}

    def getenv(key: str, default: str | None = None) -> str | None:
        return values.get(key, default)

    models = profile_models(getenv)
    assert models["qwen3_4b_instruct"] == "qwen3:4b-instruct"
    assert models["local_llm"] == "qwen3:4b-instruct"
    assert models["remote_llm"] == "qwen3:4b-instruct"

    values["REMOTE_LLM_MODEL"] = "remote:model"
    values["LOCAL_LLM_MODEL_QWEN3_4B_INSTRUCT"] = "local:model"
    models = profile_models(getenv)
    assert models["qwen3_4b_instruct"] == "local:model"
    assert models["remote_llm"] == "remote:model"


def test_profile_labels_allow_custom_legacy_labels() -> None:
    def getenv(key: str, default: str | None = None) -> str | None:
        return "Custom" if key == "LOCAL_LLM_LABEL" else default

    labels = profile_labels(getenv)
    assert labels["qwen3_14b"] == "Qwen 3 14B"
    assert labels["local_llm"] == "Custom"
