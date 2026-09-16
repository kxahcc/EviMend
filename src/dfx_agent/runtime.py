"""Non-invasive readiness checks; no installation or device commands."""

import importlib.util
import os
import shutil


def doctor() -> dict:
    commands = {name: shutil.which(name) for name in ("python", "node", "java", "hdc", "ohpm", "hvigorw")}
    modules = {name: importlib.util.find_spec(name) is not None for name in ("hypium", "xdevice", "devicetest")}
    return {
        "status": "environment_observation", "commands": commands, "python_modules": modules,
        "sdk_environment_present": {name: bool(os.environ.get(name)) for name in ("DEVECO_SDK_HOME", "OHOS_SDK_HOME", "HARMONYOS_SDK_HOME")},
        "model_key_environment_present": {name: bool(os.environ.get(name)) for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY")},
        "missing_path_commands": [name for name in ("hdc", "ohpm", "hvigorw") if not commands[name]],
        "device_connection": "not_checked", "build_ready": "not_verified", "model_provider": "not_configured",
        "limitations": ["PATH and environment checks do not exhaust installed SDKs or account access", "No model, installation or device operation was executed"],
    }
