"""Genres build_run_playlist can draw from, and the artists behind each.

`bpm` is where most of a genre's tracks sit, as ReccoBeats measures tempo. Paces
of 3:30-9:00/km mean cadences of 150-184 steps per minute, so a genre has to sit
there (one step per beat) or at half that, 75-92 (two steps per beat). House,
techno, trance and mainstream EDM sit at 120-135 and fit neither, so they're
left out.

The first four artists of each genre are shown to the runner as examples.
"""

GENRES = {
    # One step per beat.
    "drum and bass": {
        "bpm": (165, 180),
        "artists": [
            "Pendulum", "Sub Focus", "Chase & Status", "Netsky",
            "Wilkinson", "Hybrid Minds", "Dimension", "High Contrast", "Culture Shock",
        ],
    },
    "hard techno": {
        "bpm": (140, 165),
        "artists": [
            "Sara Landry", "I Hate Models", "Funk Tribu", "Nico Moreno",
            "Indira Paganotto", "Dax J", "Trym",
        ],
    },
    "hardstyle": {
        "bpm": (145, 160),
        "artists": ["Headhunterz", "Sub Zero Project", "Brennan Heart", "Da Tweekaz", "Wildstylez"],
    },
    "melodic edm": {
        "bpm": (140, 160),
        "artists": ["Illenium", "Seven Lions", "Said the Sky", "Flume", "San Holo"],
    },
    # Two steps per beat (half-time).
    "reggaeton": {
        "bpm": (75, 92),
        "artists": [
            "KAROL G", "J Balvin", "Nicky Jam", "Rauw Alejandro",
            "Zion & Lennox", "Justin Quiles", "Jhayco", "Myke Towers", "Wisin & Yandel",
        ],
    },
    "pop": {
        "bpm": (75, 92),
        "artists": [
            "The Weeknd", "Sabrina Carpenter", "Ariana Grande", "Post Malone",
            "Olivia Rodrigo", "Taylor Swift", "Bruno Mars", "Benson Boone",
            "Justin Bieber", "Doja Cat", "Chappell Roan",
        ],
    },
    "lo-fi": {
        "bpm": (75, 92),
        "artists": ["Kupla", "Philanthrope", "Sleepy Fish", "Psalm Trees", "Nymano", "idealism", "Tomppabeats"],
    },
    "downtempo": {
        "bpm": (75, 92),
        "artists": ["Massive Attack", "Thievery Corporation", "Emancipator", "Nujabes"],
    },
}
