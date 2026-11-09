LOCKS = {}


def acquire_scoped_lock(scope, identity, metadata=None):
    key = (scope, identity)
    if key in LOCKS:
        return False, {"pid": LOCKS[key]}
    LOCKS[key] = "test"
    return True, None


def release_scoped_lock(scope, identity):
    LOCKS.pop((scope, identity), None)
