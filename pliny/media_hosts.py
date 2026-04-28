"""Recognized media-host classifier. Populated when the snapshot stage ships
(build-order step 10). Until then the API ingest path treats every URL as a
generic `url` item; snapshot-time mutation to `audio`/`video` is deferred.
"""

RECOGNIZED_MEDIA_HOSTS: dict[str, str] = {
    # host suffix -> item type ('audio' or 'video')
    # populated in step 10
}
