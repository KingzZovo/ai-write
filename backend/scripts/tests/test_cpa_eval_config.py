import asyncio
from argparse import Namespace

import pytest
import yaml

from scripts.cpa_eval_config import select_direct_upstream
from scripts.evaluate_editorial_prose import main


def write_config(tmp_path, entries=None, **extra):
    entry = {'name': 'test', 'base-url': 'https://example.invalid/v1',
             'api-key-entries': [{'api-key': 'private-test-key'}],
             'models': [{'name': 'actual-model', 'alias': 'public-alias'}]}
    p = tmp_path / 'config.yaml'
    p.write_text(yaml.safe_dump({'openai-compatibility': [entry] if entries is None else entries, **extra}))
    return p, entry


def test_alias_maps_to_upstream_without_disclosing_credentials(tmp_path):
    p, _ = write_config(tmp_path, **{'proxy-url': 'http://secret-proxy.invalid:1'})
    selected = select_direct_upstream(p, 'test', 'public-alias')
    assert selected.model == 'actual-model'
    assert selected.public_metadata()['transport'] == 'direct_upstream'
    assert selected.public_metadata()['cpa_proxy_configured'] is True
    assert selected.public_metadata()['cpa_proxy_applied'] is False
    assert 'private-test-key' not in repr(selected)
    assert 'example.invalid' not in repr(selected)
    assert 'secret-proxy' not in repr(selected.public_metadata())


@pytest.mark.parametrize('flag', [True, 'false', None, 0])
def test_disabled_or_invalid_flag_rejected(tmp_path, flag):
    p, entry = write_config(tmp_path)
    entry['disabled'] = flag
    write_config(tmp_path, [entry])
    with pytest.raises(ValueError, match='disabled'):
        select_direct_upstream(p, 'test', 'public-alias')


def test_duplicate_names_are_not_silently_resolved(tmp_path):
    p, entry = write_config(tmp_path)
    write_config(tmp_path, [entry, entry])
    with pytest.raises(ValueError, match='exactly one configuration'):
        select_direct_upstream(p, 'test', 'public-alias')


@pytest.mark.parametrize('model', ['missing', 'actual-model'])
def test_unknown_or_hidden_model_rejected(tmp_path, model):
    p, _ = write_config(tmp_path)
    with pytest.raises(ValueError, match='configured public model'):
        select_direct_upstream(p, 'test', model)


def test_blank_alias_uses_name(tmp_path):
    p, entry = write_config(tmp_path)
    entry['models'][0]['alias'] = ''
    write_config(tmp_path, [entry])
    assert select_direct_upstream(p, 'test', 'actual-model').model == 'actual-model'


@pytest.mark.parametrize('field,value', [('api-key-entries', []), ('base-url', 'file:///tmp/secret')])
def test_invalid_transport_config_rejected(tmp_path, field, value):
    p, entry = write_config(tmp_path)
    entry[field] = value
    write_config(tmp_path, [entry])
    with pytest.raises(ValueError):
        select_direct_upstream(p, 'test', 'public-alias')


def test_bad_yaml_error_does_not_echo_secret(tmp_path):
    p = tmp_path / 'bad.yaml'
    p.write_text('secret-key: [private-test-key')
    with pytest.raises(ValueError) as error:
        select_direct_upstream(p, 'test', 'model')
    assert 'private-test-key' not in str(error.value)


def test_editorial_disabled_preflight_before_credentials_or_database(tmp_path):
    p, entry = write_config(tmp_path)
    entry['disabled'] = True
    write_config(tmp_path, [entry])
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('写小说')
    cases = tmp_path / 'cases.json'
    cases.write_text('[{"id":"one","brief":"一个场景"}]')
    out = tmp_path / 'output'
    args = Namespace(database_url='postgresql+asyncpg://localhost/aiwrite_quality_eval',
                     output=str(out), candidate_prompt=str(prompt), cases=str(cases),
                     cpa_config=str(p), cpa_upstream='test', model='public-alias')
    # No credentials_dir exists on args: reaching it would fail this test.
    with pytest.raises(ValueError, match='disabled'):
        asyncio.run(main(args))
    assert not out.exists()


@pytest.mark.parametrize('module', ['evaluate_summary_integrity', 'evaluate_chapter_entities'])
def test_other_probe_preflight_before_provider_or_source_access(tmp_path, module):
    import importlib
    p, entry = write_config(tmp_path)
    entry['disabled'] = True
    write_config(tmp_path, [entry])
    args = Namespace(cpa_config=str(p), upstream='test', model='public-alias')
    with pytest.raises(ValueError, match='disabled'):
        asyncio.run(importlib.import_module('scripts.' + module).main(args))
