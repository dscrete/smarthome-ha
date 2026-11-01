"""Search-result choice for spoken music requests."""


def track(name, *artists, version="", uri=None, favorite=False):
    return {"media_type": "track", "name": name, "version": version, "favorite": favorite,
            "uri": uri or f"spotify://track/{name}-{'-'.join(artists)}".replace(" ", "_"),
            "artists": [{"name": artist, "uri": f"spotify://artist/{artist}"} for artist in artists]}


def test_normalize_drops_decoration(media_match):
    assert media_match.normalize("The Beatles") == "beatles"
    assert media_match.normalize("Hey Jude (Remastered 2015)") == "hey jude"
    assert media_match.normalize("Song - 2011 Remaster") == "song"
    assert media_match.normalize("Beyoncé & Jay-Z") == "beyonce and jay z"
    assert media_match.normalize("Don't Stop Me Now") == "dont stop me now"


def test_exact_title_wins_over_live_and_karaoke(media_match):
    items = [track("Hello", "Adele", version="Live at the Royal Albert Hall"),
             track("Hello (Karaoke Version)", "Sing King"),
             track("Hello", "Adele")]
    choice = media_match.choose("track", "hello", items, artist="adele")
    assert choice["item"] is items[2]


def test_requested_variant_is_not_penalised(media_match):
    items = [track("Hello", "Adele"), track("Hello", "Adele", version="Live")]
    choice = media_match.choose("track", "hello live", items, artist="adele")
    assert choice["item"] is items[1]


def test_artist_must_match_when_named(media_match):
    items = [track("Hello", "Lionel Richie")]
    choice = media_match.choose("track", "hello", items, artist="adele")
    assert choice["item"] is None


def test_without_artist_first_confident_result_wins(media_match):
    items = [track("Hello", "Adele"), track("Hello", "Lionel Richie")]
    assert media_match.choose("track", "hello", items)["item"] is items[0]


def test_unrelated_results_are_not_played(media_match):
    items = [track("Something Completely Different", "Band")]
    choice = media_match.choose("track", "bohemian rhapsody", items)
    assert choice["item"] is None
    assert choice["suggestion"] is None


def test_misheard_title_still_matches(media_match):
    items = [track("Bohemian Rhapsody", "Queen")]
    assert media_match.choose("track", "bohemian rap city", items)["item"] is items[0]


def test_close_miss_offers_suggestion_instead_of_playing(media_match):
    items = [track("Hello", "Adele")]
    choice = media_match.choose("track", "halo", items)
    assert choice["item"] is None
    assert choice["suggestion"]["name"] == "Hello"


def test_spoken_numbers_match_digits(media_match):
    items = [{"media_type": "radio", "name": "BBC Radio 1", "uri": "tunein://radio/1"}]
    assert media_match.choose("radio", "radio one", items)["item"]["uri"] == "tunein://radio/1"


def test_artist_kind_matches_name(media_match):
    items = [{"media_type": "artist", "name": "Taylor Swift Tribute Band", "uri": "x://1"},
             {"media_type": "artist", "name": "Taylor Swift", "uri": "x://2"}]
    assert media_match.choose("artist", "taylor swift", items)["item"]["uri"] == "x://2"


def test_radio_station_partial_name(media_match):
    items = [{"media_type": "radio", "name": "BBC Radio 2", "uri": "tunein://radio/2"},
             {"media_type": "radio", "name": "BBC Radio 1", "uri": "tunein://radio/1"}]
    assert media_match.choose("radio", "bbc radio 1", items)["item"]["uri"] == "tunein://radio/1"


def test_library_favourite_breaks_ties(media_match):
    provider = {"media_type": "radio", "name": "Heart London", "uri": "tunein://radio/heart"}
    library = {"media_type": "radio", "name": "Heart London", "uri": "library://radio/7", "favorite": True}
    assert media_match.choose("radio", "heart london", [provider, library])["item"] is library


def test_items_without_uri_are_ignored(media_match):
    assert media_match.choose("track", "hello", [{"name": "Hello"}])["item"] is None


def test_split_by_artist(media_match):
    assert media_match.split_by_artist("Hello by Adele") == ("Hello", "Adele")
    assert media_match.split_by_artist("hello") is None
    # Titles containing "by" also split; the broker only tries the split after
    # the unsplit query found no confident match, so "Stand By Me" plays.
    assert media_match.split_by_artist("Stand By Me") == ("Stand", "Me")


def test_describe_is_speakable(media_match):
    item = track("Hello", "Adele", version="")
    item["album"] = {"name": "25"}
    described = media_match.describe(item)
    assert described == {"name": "Hello", "artists": ["Adele"], "album": "25", "version": None,
                         "media_type": "track", "uri": item["uri"]}
