"""Read-only CPA selection for explicit direct-upstream evaluation probes.

This is not a CPA client: proxy, routing, failover and key rotation are not
reproduced. Never serialize the selection itself; use public_metadata().
"""
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml


@dataclass(frozen=True)
class DirectSelection:
    upstream: str
    requested_model: str
    model: str
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    cpa_proxy_configured: bool = False

    def public_metadata(self):
        return dict(transport='direct_upstream', upstream=self.upstream,
                    requested_model=self.requested_model, upstream_model=self.model,
                    cpa_proxy_configured=self.cpa_proxy_configured,
                    cpa_proxy_applied=False)


def select_direct_upstream(config_path, name, model):
    """Reject unavailable/ambiguous selections before DB or network side effects."""
    try:
        config = yaml.safe_load(Path(config_path).read_text())
    except (OSError, yaml.YAMLError):
        raise ValueError('Cannot read CPA configuration') from None
    if not isinstance(config, dict):
        raise ValueError('Invalid CPA configuration')
    entries = config.get('openai-compatibility')
    if not isinstance(entries, list):
        raise ValueError('Missing CPA upstream list')
    matches = [u for u in entries if isinstance(u, dict) and u.get('name') == name]
    if len(matches) != 1:
        raise ValueError('CPA upstream must match exactly one configuration entry')
    upstream = matches[0]
    if upstream.get('disabled', False) is not False:
        raise ValueError('CPA upstream is disabled or has an invalid disabled flag')
    models = upstream.get('models', [])
    if not isinstance(models, list):
        raise ValueError('Invalid CPA model list')
    matches = [m for m in models if isinstance(m, dict)
               and (m.get('alias') or m.get('name')) == model]
    if len(matches) != 1 or not isinstance(matches[0].get('name'), str) or not matches[0]['name'].strip():
        raise ValueError('Requested model must match exactly one configured public model')
    keys = upstream.get('api-key-entries', [])
    if not isinstance(keys, list) or not keys or not isinstance(keys[0], dict):
        raise ValueError('Missing CPA upstream credential')
    key = keys[0].get('api-key')
    base = upstream.get('base-url')
    if not isinstance(key, str) or not key.strip():
        raise ValueError('Missing CPA upstream credential')
    try:
        parsed = urlsplit(base) if isinstance(base, str) else None
        valid_url = parsed and parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError('Invalid CPA upstream URL')
    return DirectSelection(name, model, matches[0]['name'], base, key,
                           bool(config.get('proxy-url') or upstream.get('proxy-url') or keys[0].get('proxy-url')))
