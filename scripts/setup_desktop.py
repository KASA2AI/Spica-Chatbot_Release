#!/usr/bin/env python3
"""Prepare a standalone desktop environment and first-run text configuration.

No network, installation, or config changes without the corresponding option.
Existing application configuration is never replaced. No services are installed.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def initial_config(model, api_base=None):
    from spica.config.application_settings import validate_api_base_url
    from spica.config.schema import AppConfig
    validate_api_base_url(api_base)
    if not model or not model.strip():
        raise ValueError('请填写服务商提供的聊天模型 ID。')
    config = dict(platform={'os': 'auto'}, llm={'model': model.strip(), 'base_url': api_base},
        character={'package_dir': 'Desktop-Packs/Characters/Examples/static', 'profile_override': None},
        tts={'enabled': False, 'daily_enabled': False},
        stt={'backend': 'qwen_asr', 'model': 'models/stt/Qwen3-ASR-1.7B', 'device': 'cpu',
             'compute_type': 'float32', 'mic_backend': 'generic', 'warmup_on_startup': False},
        memory={'consolidation_enabled': False}, screen={'enabled': False},
        song={'enabled': False}, anime={'enabled': False}, galgame={'reaction_mode': 'off'},
        home={'enabled': False})
    AppConfig.model_validate(config)
    return config


def write_initial_config(path, config):
    import yaml
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('请使用普通文件目录；不通过符号链接写入首次配置。')
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: this is bootstrap, not an upgrade/reset utility.
    with path.open('x', encoding='utf-8') as output:
        output.write(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='Create .venv-desktop and install base dependencies')
    parser.add_argument('--environment', type=Path, default=ROOT / '.venv-desktop')
    parser.add_argument('--write-config', action='store_true', help='Create first-run app.yaml only if absent')
    parser.add_argument('--model', help='Your chat provider model ID (required with --write-config)')
    parser.add_argument('--api-base', help='OpenAI-compatible API URL; omit for OpenAI')
    args = parser.parse_args(argv)
    if not args.install and not args.write_config:
        parser.print_help()
        return 0
    environment = args.environment.resolve()
    if environment in {ROOT, Path(sys.prefix).resolve()}:
        parser.error('请选择独立虚拟环境，不可使用项目根目录或当前 Python 环境。')
    if environment.exists() and not (environment / 'pyvenv.cfg').is_file():
        parser.error('目标目录已存在且不是 Python 虚拟环境，未覆盖。')
    python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if args.install:
        if not environment.exists():
            venv.EnvBuilder(with_pip=True).create(environment)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r',
                        str(ROOT / 'docs/requirements/requirements-windows-base.txt')], check=True)
    if args.write_config:
        # Configuration validation uses the environment just installed when needed.
        if args.install:
            command = [str(python), str(Path(__file__).resolve()), '--write-config', '--environment', str(environment)]
            if args.model:
                command += ['--model', args.model]
            if args.api_base:
                command += ['--api-base', args.api_base]
            return subprocess.run(command, check=False).returncode
        sys.path.insert(0, str(ROOT))
        try:
            write_initial_config(ROOT / 'data/config/app.yaml', initial_config(args.model, args.api_base))
        except (ValueError, FileExistsError) as exc:
            parser.error(str(exc) + '\n已有用户请保留原配置，在设置中修改。')
        print('已创建首次文字配置；示例角色、Home 和本地模型均不需要联网加载。')
    print(f'Python: {python}\n启动：使用此 Python 运行 {ROOT / "webui_qt.py"}')
    print('API Key 请在设置中保存。脚本没有启动应用、服务、音频或设备。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
