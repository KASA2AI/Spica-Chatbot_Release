"""Prepare pinned Windows RGB profiles without enumerating or controlling hardware.

The catalogue was checked against the official OpenRGB 1.0 source and Windows
81bbe18 binary with every detector disabled. Unknown builds require a new audit.
Linux profiles are never changed. Existing different Windows profiles are kept.
"""
import argparse
import hashlib
import json
from pathlib import Path


EXE_SHA256 = '86a88b99f60a086e13e6f6ecfb8260a94dbcd598ddeabffda7070d78c97d7e95'
CATALOG_SHA256 = '66aafb6e9e948561f93929b870b31e604f07431de9115010ebed9f741062cd5e'
DETECTORS = {
    'openrgb-windows': 'Lian Li Uni Hub - SL Infinity',
    'openrgb-ram-windows': 'ENE SMBus DRAM',
    'openrgb-motherboard-windows': 'ASUS Aura Motherboard',
}


def prepare(executable: Path, data_directory: Path) -> list[Path]:
    if executable.suffix.lower() != '.exe' or hashlib.sha256(executable.read_bytes()).hexdigest() != EXE_SHA256:
        raise ValueError('Expected the audited official OpenRGB 1.0 Windows x64 executable')
    catalog = json.loads(Path(__file__).with_name('openrgb_1_0_catalog.json').read_text('utf-8'))
    names = catalog['detectors']
    digest = hashlib.sha256(json.dumps(names, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    if (digest != CATALOG_SHA256 or len(names) != 1891 or names != sorted(set(names))
            or catalog['executable_sha256'] != EXE_SHA256):
        raise ValueError('Pinned detector catalogue has changed')
    marker = {key: catalog[key] for key in ('executable_sha256', 'detectors_sha256', 'source_commit')}
    marker['catalog_count'] = len(names)
    pending = []
    for folder, detector in DETECTORS.items():
        document = {
            'Detectors': {'detectors': {name: name == detector for name in names}},
            'Client': {'clients': []},
            'QMKOpenRGBDevices': {'devices': []},
            'QMKVialRGBDevices': {'devices': []},
            'Server': {'all_controllers': False, 'default_host': '127.0.0.1',
                       'default_port': 6742, 'legacy_workaround': False},
        }
        target = data_directory / folder
        target.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or getattr(target.stat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('RGB profile directory cannot be a reparse point')
        for filename, expected in (('OpenRGB.json', document), ('WindowsOpenRGB.json', marker)):
            path = target / filename
            if path.is_symlink() or (path.exists() and getattr(path.stat(), 'st_file_attributes', 0) & 0x400):
                raise ValueError('RGB profile cannot be a reparse point')
            if path.exists():
                if json.loads(path.read_text('utf-8')) != expected:
                    raise ValueError(f'Existing RGB profile differs; preserved: {path}')
            else:
                pending.append((path, expected))
    for path, document in pending:
        with path.open('x', encoding='utf-8') as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    return [data_directory / folder for folder in DETECTORS]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True, type=Path)
    parser.add_argument('--data-directory', required=True, type=Path)
    options = parser.parse_args()
    for prepared in prepare(options.executable.resolve(strict=True), options.data_directory.resolve(strict=True)):
        print(prepared)
