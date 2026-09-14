import sys


if __name__ == "__main__":
    if '--collection-worker' in sys.argv:
        from shaq_daily_oracle.collection_worker import main
        raise SystemExit(main())
    if '--model-http-worker' in sys.argv:
        # The transport child must never initialize Velopack, the GUI, or user-data services.
        from shaq_daily_oracle.model_http_worker import main
        raise SystemExit(main())
    # Python App.run has no locator override. Explicit synthetic acceptance
    # modes must not inspect/clean the real user's native package cache.
    acceptance = any(flag in sys.argv for flag in ('--smoke', '--gui-smoke', '--update-smoke'))
    if getattr(sys, 'frozen', False) and not acceptance:
        import velopack
        # Fast-exit install/update hooks must run before importing the desktop.
        # Downloaded updates never auto-apply outside the admission gate.
        velopack.App().set_auto_apply_on_startup(False).run()
    if '--update-smoke' in sys.argv:
        from shaq_daily_oracle.update_smoke import main
        raise SystemExit(main())
    from shaq_daily_oracle.desktop import main
    raise SystemExit(main())
