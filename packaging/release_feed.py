"""Prepare one verified public full package for the final native delta build."""
from contextlib import nullcontext
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace

import httpx
from packaging.version import Version

from shaq_daily_oracle.software_updates import CHANNELS, UpdateRuntime
from shaq_daily_oracle.update_native import verify_cached


RECEIPT_NAME = 'delta-base.{channel}.json'
RELEASES_PER_PAGE = 100
HTTP_TIMEOUT_SECONDS = 60


def prepare_public_base(root, output, version, system, machine, *, client=None):
    """Only an empty final pack directory may receive a previous public base.

    Velopack requires the previous release in outputDir to generate deltas:
    https://docs.velopack.io/packaging/deltas
    Internal consecutive-update acceptance deliberately does not call this step.
    """
    output = Path(output)
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError('Public base preparation requires a fresh empty output directory')
    repo = json.loads((root / 'config/team-repository.json').read_text())
    if not all(re.fullmatch(r'[A-Za-z0-9_.-]+', repo[key]) for key in ('owner', 'repository')):
        raise ValueError('Invalid official repository configuration')
    repository = repo['owner'] + '/' + repo['repository']
    updates = json.loads((root / 'config/software-updates.json').read_text())
    toolchain = json.loads((root / 'packaging/updater-toolchain.json').read_text())
    internal = {toolchain['acceptance_prior_version'], toolchain['acceptance_bridge_version']}
    if not re.fullmatch(r'\d+\.\d+\.\d+', version) or version in internal:
        raise ValueError('A final feed requires a public target, not an acceptance fixture version')
    key = system + '/' + machine.lower()
    channel = updates['channels'][key]
    suffix = CHANNELS[(system, machine.lower())][0]
    feed_name = 'releases.' + channel + '.json'
    receipt = dict(status='first-managed-release', target_version=version, channel=channel,
                   repository=repository, base_version=None,
                   fallback='full package for clients without the selected base')
    # A new unauthenticated client: no app/user token, Git credential or GitHub CLI state.
    with (nullcontext(client) if client is not None else httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)) as request:
        candidates = []
        page = 1
        while True:
            response = request.get(f'https://api.github.com/repos/{repository}/releases',
                                   params={'per_page': RELEASES_PER_PAGE, 'page': page},
                                   headers={'Accept': 'application/vnd.github+json'})
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError('Invalid public release list')
            for row in rows:
                match = re.fullmatch(r'lab-v(\d+\.\d+\.\d+)-' + suffix, str(row.get('tag_name', '')))
                if row.get('draft') or not match or match[1] in internal or Version(match[1]) >= Version(version):
                    continue
                feeds = [asset for asset in row.get('assets', []) if asset.get('name') == feed_name]
                if not feeds:
                    continue  # Legacy/no-such-architecture release, not a network failure.
                base = f'https://github.com/{repository}/releases/download/{row["tag_name"]}/'
                if (len(feeds) != 1 or feeds[0].get('browser_download_url') != base + feed_name or
                        row.get('html_url') != f'https://github.com/{repository}/releases/tag/{row["tag_name"]}'):
                    raise ValueError('Advertised channel feed does not belong to the official release')
                candidates.append((Version(match[1]), row, base))
            if len(rows) < RELEASES_PER_PAGE:
                break
            page += 1
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='shaq-public-base-', dir=output.parent) as directory:
            staged = Path(directory)
            if candidates:
                prior, row, base = max(candidates, key=lambda item: item[0])
                response = request.get(base + feed_name, follow_redirects=True)
                response.raise_for_status()
                feed = response.json()
                assets = feed.get('Assets') if isinstance(feed, dict) else None
                if not isinstance(assets, list) or not assets:
                    raise ValueError('Advertised public feed is empty or invalid')
                seen = set()
                for asset in assets:
                    UpdateRuntime._validate_asset(asset, updates['package_id'], channel, str(prior))
                    if asset['FileName'] in seen or asset['Version'] in internal:
                        raise ValueError('Public feed contains duplicate or internal acceptance packages')
                    seen.add(asset['FileName'])
                full = [asset for asset in assets if asset['Type'] == 'Full' and asset['Version'] == str(prior)]
                if len(full) != 1:
                    raise ValueError('Public release has no unambiguous same-version full package')
                asset = full[0]
                published = [item for item in row['assets'] if item.get('name') == asset['FileName']]
                if (len(published) != 1 or published[0].get('browser_download_url') != base + asset['FileName'] or
                        published[0].get('size') != asset['Size']):
                    raise ValueError('Public full package URL/size does not match the release feed')
                with request.stream('GET', base + asset['FileName'], follow_redirects=True) as response:
                    response.raise_for_status()
                    with (staged / asset['FileName']).open('xb') as stream:
                        size = 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > asset['Size']:
                                raise ValueError('Public full package exceeds advertised size')
                            stream.write(chunk)
                verify_cached(staged, SimpleNamespace(**asset))
                (staged / feed_name).write_text(json.dumps({'Assets': [asset]}), encoding='utf-8')
                receipt.update(status='public-base', base_version=str(prior),
                               source_url=base + asset['FileName'], filename=asset['FileName'],
                               sha256=asset['SHA256'], size=asset['Size'])
            (staged / RECEIPT_NAME.format(channel=channel)).write_text(json.dumps(receipt, indent=2), encoding='utf-8')
            output.mkdir(exist_ok=True)
            for path in staged.iterdir():
                path.replace(output / path.name)
    return receipt
