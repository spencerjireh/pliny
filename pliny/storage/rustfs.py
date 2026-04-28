class RustFSBlobStore:
    """Stub. Implementation lands with the snapshot slice (build-order step 10)."""

    def __init__(self, *, endpoint: str, access_key: str, secret_key: str, bucket: str) -> None:
        self._endpoint = endpoint
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket

    async def put(self, key: str, data: bytes) -> None:
        raise NotImplementedError("RustFS impl ships with snapshot slice")

    async def get(self, key: str) -> bytes:
        raise NotImplementedError("RustFS impl ships with snapshot slice")

    async def exists(self, key: str) -> bool:
        raise NotImplementedError("RustFS impl ships with snapshot slice")

    async def delete(self, key: str) -> None:
        raise NotImplementedError("RustFS impl ships with snapshot slice")

    def url_for(self, key: str) -> str | None:
        raise NotImplementedError("RustFS impl ships with snapshot slice")
