#!/usr/bin/env python3
"""Prepare a standalone desktop environment and first-run text configuration.

No network, installation, or config changes without the corresponding option.
--write-config never replaces an existing file. --configure-text explicitly
selects the bundled sample and disables optional features in an existing config.
No services are installed.
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
    config_action = parser.add_mutually_exclusive_group()
    config_action.add_argument('--write-config', action='store_true', help='Create first-run app.yaml only if absent')
    config_action.add_argument('--configure-text', action='store_true', help='Select the bundled sample and save a text-only setup; preserves unrelated settings')
    parser.add_argument('--model', help='Your chat provider model ID (required when configuring text)')
    parser.add_argument('--api-base', help='OpenAI-compatible API URL; omit for OpenAI')
    args = parser.parse_args(argv)
    if not args.install and not args.write_config and not args.configure_text:
        parser.print_help()
        return 0
    environment = args.environment.resolve()
    if args.install and environment in {ROOT, Path(sys.prefix).resolve()}:
        parser.error('请选择独立虚拟环境，不可使用项目根目录或当前 Python 环境。')
    if args.install and environment.exists() and not (environment / 'pyvenv.cfg').is_file():
        parser.error('目标目录已存在且不是 Python 虚拟环境，未覆盖。')
    python = (environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
              if args.install else Path(sys.executable))
    if args.install:
        if not environment.exists():
            venv.EnvBuilder(with_pip=True).create(environment)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r',
                        str(ROOT / 'docs/requirements/requirements-windows-base.txt')], check=True)
    if args.write_config or args.configure_text:
        # Configuration validation uses the environment just installed when needed.
        if args.install:
            mode = '--write-config' if args.write_config else '--configure-text'
            command = [str(python), str(Path(__file__).resolve()), mode]
            if args.model:
                command += ['--model', args.model]
            if args.api_base:
                command += ['--api-base', args.api_base]
            return subprocess.run(command, check=False).returncode
        sys.path.insert(0, str(ROOT))
        try:
            target = ROOT / 'data/config/app.yaml'
            if args.write_config and target.exists():
                raise FileExistsError(str(target))
            config = initial_config(args.model, args.api_base)
            from spica.host.character_packages import import_character_folder
            package = import_character_folder(
                ROOT / 'Desktop-Packs/Characters/Examples/static', ROOT / 'data/runtime/characters',
            )
            config['character']['package_dir'] = package.package_root
            if args.configure_text:
                from spica.config.manager import ConfigManager
                ConfigManager(target).update(config)
            else:
                write_initial_config(target, config)
        except (ValueError, FileExistsError) as exc:
            parser.error(str(exc) + '\n已有用户可在设置中修改；首次配置可显式使用 --configure-text 选择静态示例并关闭可选功能。')
        print('已导入静态示例并保存文字配置；Home 和本地模型均未启用。')
    print(f'Python: {python}\n启动：使用此 Python 运行 {ROOT / "webui_qt.py"}')
    print('API Key 请在设置中保存。脚本没有启动应用、服务、音频或设备。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
