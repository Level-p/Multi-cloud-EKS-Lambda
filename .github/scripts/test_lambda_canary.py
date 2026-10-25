"""lambda_canary.sh passes the alias routing as JSON: the AWS CLI shorthand cannot quote a version number
(AdditionalVersionWeights={"4"=0.1} fails with ParamValidation, after the new version is already published)."""
import json
import re
from pathlib import Path

SCRIPT = (Path(__file__).parent / "lambda_canary.sh").read_text(encoding="utf-8")


def routing_configs():
    """Every --routing-config argument, as the shell would pass it (variables filled in)."""
    for m in re.finditer(r"--routing-config\s+(\"(?:[^\"\\]|\\.)*\"|'[^']*')", SCRIPT):
        arg = m.group(1)
        if arg.startswith("'"):
            yield arg[1:-1]
        else:
            yield arg[1:-1].replace('\\"', '"').replace("$NEW", "4").replace("$CANARY_WEIGHT", "0.1")


def test_every_routing_config_is_json_with_string_versions():
    configs = list(routing_configs())
    assert len(configs) == 3
    for c in configs:
        weights = json.loads(c)["AdditionalVersionWeights"]
        assert all(isinstance(v, str) and isinstance(w, float) for v, w in weights.items())
    assert {"4": 0.1} in [json.loads(c)["AdditionalVersionWeights"] for c in configs]
