"""Domain assembly convention (OO migration Phase 4).

One module per domain: ``spica/host/assemblies/<domain>.py`` exposing
``install(host)`` plus its build functions. Installers live INSIDE the host
package (铁律 #7 -- platform wiring never leaves the host boundary) and MUST
build through the AppHost thin-delegate methods: the facade is the ONLY build
path, so ``patch.object(AppHost, "_new_...")`` in tests keeps intercepting real
construction (patch-validity tests pin this per domain). Write-authority
closures stay on the host itself (铁律 #9); policy/decision logic goes into the
domain package (e.g. ``spica/galgame/reaction_scoring.py``).
"""
