"""Report each unavailable softball source without suppressing permitted ones."""


def softball_classes(manifest):
    from common.softball_registry import softball_registry
    result = []
    for cls in softball_registry().values():
        try:
            if hasattr(cls, 'require_ready'):
                cls.require_ready()
            cls.require_permission()
            result.append(cls)
        except (RuntimeError, NotImplementedError) as exc:
            manifest.provider(cls.__name__, "queued", error=str(exc))
    return result
