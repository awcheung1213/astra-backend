import enum
from typing import NamedTuple


# Census state FIPS -> USPS abbrev (50 states + DC + territories); shared by
# market seeding and radius-based footprint expansion
STATE_ABBREV_BY_FIPS = {
    "01": "AL",
    "02": "AK",
    "04": "AZ",
    "05": "AR",
    "06": "CA",
    "08": "CO",
    "09": "CT",
    "10": "DE",
    "11": "DC",
    "12": "FL",
    "13": "GA",
    "15": "HI",
    "16": "ID",
    "17": "IL",
    "18": "IN",
    "19": "IA",
    "20": "KS",
    "21": "KY",
    "22": "LA",
    "23": "ME",
    "24": "MD",
    "25": "MA",
    "26": "MI",
    "27": "MN",
    "28": "MS",
    "29": "MO",
    "30": "MT",
    "31": "NE",
    "32": "NV",
    "33": "NH",
    "34": "NJ",
    "35": "NM",
    "36": "NY",
    "37": "NC",
    "38": "ND",
    "39": "OH",
    "40": "OK",
    "41": "OR",
    "42": "PA",
    "44": "RI",
    "45": "SC",
    "46": "SD",
    "47": "TN",
    "48": "TX",
    "49": "UT",
    "50": "VT",
    "51": "VA",
    "53": "WA",
    "54": "WV",
    "55": "WI",
    "56": "WY",
    "60": "AS",
    "66": "GU",
    "69": "MP",
    "72": "PR",
    "78": "VI",
}

MAX_FOOTPRINT_CITIES = 25

# 50 states + DC + territories; the universe for market state codes
US_STATE_ABBREVS = frozenset(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "DC",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "AS",
        "GU",
        "MP",
        "PR",
        "VI",
    ]
)


class CityData(NamedTuple):
    city: str
    state: str
    zip: str


class LocationCity(enum.Enum):
    BIRMINGHAM = CityData("Birmingham", "AL", "35203")
    ANCHORAGE = CityData("Anchorage", "AK", "99501")
    PHOENIX = CityData("Phoenix", "AZ", "85001")
    LITTLE_ROCK = CityData("Little Rock", "AR", "72201")
    LOS_ANGELES = CityData("Los Angeles", "CA", "90001")
    DENVER = CityData("Denver", "CO", "80202")
    BRIDGEPORT = CityData("Bridgeport", "CT", "06604")
    WILMINGTON = CityData("Wilmington", "DE", "19801")
    JACKSONVILLE = CityData("Jacksonville", "FL", "32099")
    ATLANTA = CityData("Atlanta", "GA", "30301")
    HONOLULU = CityData("Honolulu", "HI", "96801")
    BOISE = CityData("Boise", "ID", "83701")
    CHICAGO = CityData("Chicago", "IL", "60601")
    INDIANAPOLIS = CityData("Indianapolis", "IN", "46201")
    DES_MOINES = CityData("Des Moines", "IA", "50301")
    WICHITA = CityData("Wichita", "KS", "67201")
    LOUISVILLE = CityData("Louisville", "KY", "40202")
    NEW_ORLEANS = CityData("New Orleans", "LA", "70112")
    PORTLAND_ME = CityData("Portland", "ME", "04101")
    BALTIMORE = CityData("Baltimore", "MD", "21201")
    BOSTON = CityData("Boston", "MA", "02101")
    DETROIT = CityData("Detroit", "MI", "48226")
    MINNEAPOLIS = CityData("Minneapolis", "MN", "55401")
    JACKSON = CityData("Jackson", "MS", "39201")
    KANSAS_CITY = CityData("Kansas City", "MO", "64101")
    BILLINGS = CityData("Billings", "MT", "59101")
    OMAHA = CityData("Omaha", "NE", "68102")
    LAS_VEGAS = CityData("Las Vegas", "NV", "89101")
    MANCHESTER = CityData("Manchester", "NH", "03101")
    NEWARK = CityData("Newark", "NJ", "07102")
    ALBUQUERQUE = CityData("Albuquerque", "NM", "87101")
    NEW_YORK = CityData("New York", "NY", "10001")
    CHARLOTTE = CityData("Charlotte", "NC", "28202")
    FARGO = CityData("Fargo", "ND", "58102")
    COLUMBUS = CityData("Columbus", "OH", "43215")
    OKLAHOMA_CITY = CityData("Oklahoma City", "OK", "73102")
    PORTLAND_OR = CityData("Portland", "OR", "97201")
    PHILADELPHIA = CityData("Philadelphia", "PA", "19102")
    PROVIDENCE = CityData("Providence", "RI", "02903")
    COLUMBIA = CityData("Columbia", "SC", "29201")
    SIOUX_FALLS = CityData("Sioux Falls", "SD", "57104")
    NASHVILLE = CityData("Nashville", "TN", "37201")
    HOUSTON = CityData("Houston", "TX", "77002")
    SALT_LAKE_CITY = CityData("Salt Lake City", "UT", "84101")
    BURLINGTON = CityData("Burlington", "VT", "05401")
    VIRGINIA_BEACH = CityData("Virginia Beach", "VA", "23451")
    SEATTLE = CityData("Seattle", "WA", "98101")
    CHARLESTON = CityData("Charleston", "WV", "25301")
    MILWAUKEE = CityData("Milwaukee", "WI", "53202")
    CHEYENNE = CityData("Cheyenne", "WY", "82001")


class UsageKind(str, enum.Enum):
    EXECUTE_PROMPTS = "execute_prompts"
    GENERATE_PROMPTS = "generate_prompts"
    SENTIMENT_ANALYSIS = "sentiment_analysis"
    COMPETITOR_DISCOVERY = "competitor_discovery"
    GENERATE_ACTIONS = "generate_actions"
    CLASSIFY_PROMPT = "classify_prompt"


class UsageType(str, enum.Enum):
    EXECUTION = "execution"
    BATCH = "batch"


class UsageProvider(str, enum.Enum):
    CLAUDE = "claude"
    GEMINI = "gemini"
    CHATGPT = "chatgpt"


class UsageModel(str, enum.Enum):
    GPT_5_4_NANO_2026_03_17 = "gpt-5.4-nano-2026-03-17"
    CLAUDE_HAIKU_4_5_20251001 = "claude-haiku-4-5-20251001"
    GEMINI_3_1_FLASH_LITE = "gemini-3.1-flash-lite"
    CLAUDE_OPUS_5 = "claude-opus-5"
