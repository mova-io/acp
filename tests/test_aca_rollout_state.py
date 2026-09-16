import io
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/public'))
from aca_rollout_state import main, parse  # noqa: E402


def test_real_azure_json_shape_is_parsed_without_tsv_row_assumptions(monkeypatch, capsys):
    payload = {'provisioning': 'Succeeded', 'min_replicas': 0,
               'revision': 'acp-discovery-staging--372'}
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))
    assert main() == 0
    assert json.loads(capsys.readouterr().out) == [
        'Succeeded', 0, 'acp-discovery-staging--372']


def test_the_observed_newline_tsv_array_cannot_silently_parse(monkeypatch):
    # Real `az --query '[a,b,c]' -o tsv` emitted these as three rows, not columns.
    monkeypatch.setattr(sys, 'stdin', io.StringIO('Succeeded\n0\nacp-discovery-staging--372\n'))
    assert main() == 1


def test_redeploy_has_no_multi_field_azure_tsv_read():
    source = (ROOT / 'deploy/public/redeploy.sh').read_text()
    assert "--query '[properties.provisioningState," not in source
    assert not re.search(r'read -r[^\n]*\n\$\(az .* -o tsv', source)


@pytest.mark.parametrize('payload', [
    {}, {'provisioning': 'Succeeded', 'min_replicas': 0},
    {'provisioning': 'Succeeded', 'min_replicas': True, 'revision': 'r'},
    {'provisioning': 'Succeeded', 'min_replicas': -1, 'revision': 'r'},
    {'provisioning': '', 'min_replicas': 0, 'revision': 'r'},
])
def test_partial_or_invalid_state_fails_closed(payload):
    with pytest.raises(ValueError):
        parse(payload)
