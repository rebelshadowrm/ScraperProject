# common/config.py

import os

OUTPUT_COLUMNS = [
    "Tournament-name", "Start-date", "End-date", "Are-there-multiple_locations",
    "Address", "Address-2", "Venue", "City", "State-FullName", "Zip",
    "Age-groups.1", "Entry-fee.1", "Age-groups.2", "Entry-fee.2",
    "Age-groups.3", "Entry-fee.3", "Age-groups.4", "Entry-fee.4",
    "Age-groups.5", "Entry-fee.5", "Age-groups.6", "Entry-fee.6",
    "Age-groups.7", "Entry-fee.7", "Age-groups.8", "Entry-fee.8",
    "Age-groups.9", "Entry-fee.9", "Age-groups.10", "Entry-fee.10",
    "Age-groups.11", "Entry-fee.11", "Age-groups.12", "Entry-fee.12",
    "Age-groups.13", "Entry-fee.13", "Select All Competition Levels That Apply.",
    "Competition-level.1", "Competition-level.2", "Competition-level.3",
    "Competition-level.4", "Competition-level.5", "Competition-level.6",
    "Competition-level.7", "Competition-level.8", "Competition-level.9",
    "Guaranteed-games", "Gate-fees", "Hotel-required", "Tournament-type",
    "Tournament-surface", "Contact-name", "Phone-number", "Email",
    "Website-url", "More-information-and-additional-tournament-locations"
]

# --- Canonical dropdown tokens (what we will WRITE) ---
# IMPORTANT: keep these exactly as the site expects.
VALID_LEVELS = ["Open", "AAA", "AA", "A", "D1", "D2", "D3", "Silver", "Rec"]

VALID_AGE_GROUPS = [f"{i}u" for i in range(6, 19)]  # 6u ... 18u

VALID_GUARANTEED_GAMES = ["None"] + [str(i) for i in range(1, 10)]  # None, 1..9

VALID_TOURNAMENT_TYPES = ["Team", "Individual"]

VALID_TOURNAMENT_SURFACES = ["NA", "Grass", "Artificial"]

VALID_YES_NO_NA = ["NA", "Yes", "No"]

# If you later discover the *importer* expects some weird template spacing,
# you can flip this to True and set exact tokens in normalize.py mappings.
EXPORT_CANONICAL_DROPDOWN_TOKENS = True

TEMPLATE_PATH = os.path.join("templates", "Tournaments-Import-Template.xlsx")
TEMPLATE_SHEET = "Tournament Import Template"
