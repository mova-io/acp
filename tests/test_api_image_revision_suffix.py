"""The API image PATCH must name the revision it is asking ACA to create.

Microsoft.App/containerApps update "patches a Container App using JSON Merge
Patch", so a member the body omits is left unchanged (RFC 7386) -- omitting
revisionSuffix keeps whatever explicit suffix the resource already carries. A
revision name is <app>--<suffix>, is a unique identifier, and names a revision
that is immutable once established, so an image-only rollout against a pinned
suffix asks for an existing revision under a new template.

The provider below models that: merge-patch retention, plus a refusal when a
changed template re-uses a live revision name. The refusal shape is a model of
the incident (accepted PATCH, Failed app, old image on the template, no new
revision) rather than a transcript of an ARM error, so the assertions that
matter do not depend on it: each test also pins the property we can prove
locally -- the body always carries an explicit, valid, unused suffix.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUBSCRIPTION = 'sub'
GROUP = 'group'
APP = 'acp-app'
IMAGE = 'mdkaccessibilityacr.azurecr.io/acp-app:27586d7-1789680910'
PINNED = 'r775f5ac-1789659847'


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy/public' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


update = module('update_api_image')


class Provider:
    """A container app whose PATCH is JSON Merge Patch and whose revisions are unique."""

    def __init__(self, suffix=PINNED, image='mdkaccessibilityacr.azurecr.io/acp-app:775f5ac-1789659847'):
        self.template = {
            'revisionSuffix': suffix,
            'containers': [{'name': APP, 'image': image, 'env': [{'name': 'KEEP', 'value': 'kept'}],
                            'probes': [{'type': 'Readiness', 'httpGet': {'path': '/probe/readyz', 'port': 8077},
                                        'failureThreshold': 10}],
                            'resources': {'cpu': 1, 'memory': '2Gi'}}],
            'scale': {'maxReplicas': 4},
        }
        self.state = 'Succeeded'
        self.revisions = {APP + '--' + suffix: json.dumps(self.template, sort_keys=True)}
        self.bodies = []

    def show(self):
        return {'id': (f'/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}'
                       f'/providers/Microsoft.App/containerApps/{APP}'),
                'name': APP, 'properties': {'provisioningState': self.state,
                                            'template': json.loads(json.dumps(self.template))}}

    def patch(self, body):
        self.bodies.append(body)
        merged = json.loads(json.dumps(self.template))
        for key, value in body['properties']['template'].items():
            merged[key] = value
        name = APP + '--' + merged.get('revisionSuffix', '')
        snapshot = json.dumps(merged, sort_keys=True)
        if name in self.revisions and self.revisions[name] != snapshot:
            # An immutable revision cannot be re-cut under a new template. The
            # PATCH is still accepted; the app is what fails.
            self.state = 'Failed'
            return
        self.state = 'Succeeded'
        self.template = merged
        self.revisions[name] = snapshot

    def runner(self):
        def run(command, **kwargs):
            if command[1] == 'containerapp':
                return SimpleNamespace(returncode=0, stdout=json.dumps(self.show()), stderr='')
            self.patch(json.loads(Path(command[command.index('--body') + 1][1:]).read_text()))
            return SimpleNamespace(returncode=0, stdout='{}', stderr='')
        return run


def rollout(monkeypatch, provider, image=IMAGE, suffix=None):
    monkeypatch.setattr(update.subprocess, 'run', provider.runner())
    argv = ['--subscription', SUBSCRIPTION, '--group', GROUP, '--app', APP, '--image', image]
    if suffix:
        argv += ['--revision-suffix', suffix]
    return update.main(argv)


def test_a_body_that_omits_the_suffix_reuses_the_pinned_revision_and_strands_the_image():
    """The shape this writer used to send, against the resource production was on."""
    provider = Provider()
    legacy = json.loads(json.dumps(provider.template))
    legacy.pop('revisionSuffix')
    legacy['containers'][0]['image'] = IMAGE
    provider.patch({'properties': {'template': legacy}})
    assert provider.state == 'Failed'
    assert provider.template['containers'][0]['image'] != IMAGE
    assert list(provider.revisions) == [APP + '--' + PINNED]


def test_normal_rollout_names_a_new_revision_and_lands_the_image(monkeypatch):
    provider = Provider()
    assert rollout(monkeypatch, provider) == 0
    sent = provider.bodies[-1]['properties']['template']['revisionSuffix']
    assert sent != PINNED and update.valid_suffix(APP, sent)
    assert provider.state == 'Succeeded'
    assert provider.template['containers'][0]['image'] == IMAGE
    assert provider.template['revisionSuffix'] == sent
    assert set(provider.revisions) == {APP + '--' + PINNED, APP + '--' + sent}
    assert {'name': 'KEEP', 'value': 'kept'} in provider.template['containers'][0]['env']
    assert provider.template['containers'][0]['probes'][0]['type'] == 'Readiness'
    assert update.STARTUP_PROBE in provider.template['containers'][0]['probes']


def test_redeploying_the_same_image_asks_for_a_distinct_revision(monkeypatch):
    """Recovery paths re-push an image that already has a revision of its own."""
    provider = Provider()
    assert rollout(monkeypatch, provider) == 0
    first = provider.template['revisionSuffix']
    provider.template['containers'][0]['env'].append({'name': 'ACP_DEPLOY_ENV', 'value': 'production'})
    assert rollout(monkeypatch, provider) == 0
    second = provider.template['revisionSuffix']
    assert second != first and update.valid_suffix(APP, second)
    assert provider.state == 'Succeeded' and len(provider.revisions) == 3


def test_an_explicit_blue_green_suffix_is_sent_verbatim(monkeypatch):
    provider = Provider()
    assert rollout(monkeypatch, provider, suffix='g27586d7-1789680910') == 0
    assert provider.bodies[-1]['properties']['template']['revisionSuffix'] == 'g27586d7-1789680910'
    assert provider.template['containers'][0]['image'] == IMAGE
    assert APP + '--g27586d7-1789680910' in provider.revisions


@pytest.mark.parametrize('suffix', ['27586d7-1789680910', 'g--27586d7', 'Green', 'green-', 'green\n', 'g' * 60])
def test_a_suffix_aca_would_reject_is_refused_without_a_patch(monkeypatch, capsys, suffix):
    provider = Provider()
    assert rollout(monkeypatch, provider, suffix=suffix) == 1
    assert provider.bodies == [] and provider.state == 'Succeeded'
    assert capsys.readouterr().err.strip() == 'API image update refused'


def test_the_derived_suffix_obeys_the_documented_revision_rules():
    for app, image in (('acp-app', IMAGE), ('acp-app-staging', IMAGE),
                       ('acp-app', 'reg.io/acp-app:2026.9.6.15'), ('acp-app', 'acp-app')):
        suffix = update.derived_suffix(app, image, token='7f3a2c')
        assert update.valid_suffix(app, suffix), (app, image, suffix)
        assert suffix.endswith('-7f3a2c') and suffix.startswith('r')
    assert update.derived_suffix(APP, IMAGE, token='7f3a2c') == 'r27586d7-1789680910-7f3a2c'
    assert update.derived_suffix(APP, 'reg.io/acp-app:2026.9.6.15', token='7f3a2c') == 'r2026-9-6-15-7f3a2c'
    long_app = 'a' * 50
    assert update.valid_suffix(long_app, update.derived_suffix(long_app, IMAGE, token='7f3a2c'))
    with pytest.raises(ValueError):
        update.derived_suffix('a' * 62, IMAGE, token='7f3a2c')
