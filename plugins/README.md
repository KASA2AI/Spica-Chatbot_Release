# Plugins

External capability plugins (Phase 8). A plugin is a package directory here:

```
plugins/<name>/__init__.py
```

whose `__init__.py` exposes a `register(registry)` function that registers
adapters and/or tools into the `CapabilityRegistry`:

```python
# plugins/example_tts/__init__.py
def register(registry):
    registry.register_tts("example_tts", lambda **kw: MyTTSAdapter(**kw))
```

Enable it in `data/config/plugins.yaml`:

```yaml
plugins:
  - name: example_tts
    enabled: true
```

Then select it via config (e.g. set the TTS provider to `example_tts`). Loading
happens at startup; manifest changes take effect on restart. A plugin that fails
to import or has no `register` is skipped and recorded (it never breaks startup).

This phase allows registering **adapters / tools only** — Settings UI / chat
widgets are a later phase.
