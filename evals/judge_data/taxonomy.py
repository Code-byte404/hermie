"""The judge question, the category / form / length grid and the plan of generation batches."""
from __future__ import annotations

QUESTION_ID = "private"
QUESTION = ("Does this text contain private information about a specific person, or non-public company "
            "information, that should not be sent to an outside AI service?")

# Positives: what the judge must catch (rules own credentials and contact data, so those are not the target).
POS_CATEGORIES = {
    "health": "a named or clearly identifiable person's medical condition, diagnosis, treatment or sick leave",
    "personal_finance": "a specific person's salary, debts, bank situation, tax or compensation details",
    "legal": "a lawsuit, settlement, investigation, contract dispute or legal advice about a named person or the company",
    "personal_situation": "family, relationship, immigration, housing or other private circumstances of a specific person",
    "hr": "layoffs, firing, performance reviews, promotion or hiring decisions about named employees",
    "financials_ma": "unreleased financial results, forecasts, fundraising, acquisitions, board decisions",
    "customer_data": "per-customer records, customer lists, contracts, usage or purchase data tied to named customers",
    "unreleased_product": "unannounced products, features, roadmaps, code names, launch dates, pricing plans",
    "proprietary_logic": "proprietary algorithms, scoring models, trade secrets or patent-related logic of the company",
    "internal_infra": "internal hostnames, network topology, unpatched vulnerabilities, access arrangements, incident details",
    "incident_people": "post-mortems, security incidents or complaints that name specific people or customers",
}

# Negatives: what the judge must let through. The first group is hard (same vocabulary, nothing private).
HARD_NEG_KINDS = {
    "generic_sensitive_topic": "general discussion of health, money, law, HR or security with no specific person or company",
    "code_with_keywords": "ordinary code, schemas or tests whose identifiers merely use words like salary, patient, customer, layoff",
    "public_company_info": "facts that are public: published reports, documentation, release notes, open-source changelogs",
    "fiction_hypothetical": "fiction, examples, tutorials and hypotheticals with invented, clearly generic characters",
}
EASY_NEG_KINDS = {
    "plain_code": "source code, configs and scripts with no personal or business context",
    "logs_output": "build output, stack traces, test results, package manager output, shell sessions",
    "docs_howto": "technical documentation, README text, how-to answers, API descriptions",
    "general_question": "general knowledge or programming questions a user would ask an assistant",
    "placeholders_only": "text where people and values are already placeholders such as <PERSON_1> or <EMAIL_ADDRESS_2>",
}

# Forms, with the origin they represent in Claude Code traffic.
FORMS = {
    "chat_prompt": ("user", "a message a user types to a coding assistant"),
    "markdown": ("tool_result", "a markdown file or README the assistant read"),
    "source_file": ("tool_result", "a source file with comments and strings"),
    "structured_rows": ("tool_result", "JSON, YAML or CSV rows"),
    "sql_dump": ("tool_result", "SQL statements or query output"),
    "shell_log": ("tool_result", "terminal output or a log file"),
    "git_diff": ("tool_result", "a git diff or commit message"),
    "email_thread": ("tool_result", "an email or chat thread saved in the project"),
    "ticket": ("tool_result", "an issue or ticket with comments"),
}

# Which forms suit which positive category (4 each), so a "git diff" is not forced to carry a medical record.
CATEGORY_FORMS = {
    "health": ["chat_prompt", "markdown", "structured_rows", "email_thread"],
    "personal_finance": ["chat_prompt", "structured_rows", "sql_dump", "email_thread"],
    "legal": ["chat_prompt", "markdown", "email_thread", "ticket"],
    "personal_situation": ["chat_prompt", "email_thread", "markdown", "ticket"],
    "hr": ["chat_prompt", "markdown", "structured_rows", "email_thread"],
    "financials_ma": ["chat_prompt", "markdown", "structured_rows", "email_thread"],
    "customer_data": ["structured_rows", "sql_dump", "markdown", "shell_log"],
    "unreleased_product": ["chat_prompt", "markdown", "source_file", "git_diff"],
    "proprietary_logic": ["source_file", "git_diff", "markdown", "chat_prompt"],
    "internal_infra": ["shell_log", "markdown", "source_file", "ticket"],
    "incident_people": ["ticket", "markdown", "email_thread", "shell_log"],
}

LENGTHS = {            # characters: (target low, target high); validation allows a tolerance
    "s": (80, 300),
    "m": (600, 1400),
    "l": (3000, 4500),
    "xl": (7000, 8000),
}
LENGTH_TOLERANCE = (0.5, 1.6)

# Scenarios per generation batch (a scenario is one positive + one matched hard negative), by length.
PAIR_SCENARIOS = {"s": 16, "m": 10, "l": 5}
PAIR_BATCHES = {"s": 3, "m": 5, "l": 3}        # batches per category and length
EASY_PER_BATCH = {"s": 40, "m": 24, "l": 10}
EASY_BATCHES = {"s": 24, "m": 24, "l": 8}      # batches per length, over EASY_NEG_KINDS and all forms
XL_PER_BATCH = 6                               # xl items: test split only; a private fact buried deep, and matched negatives

INDUSTRIES = ["a regional hospital network", "a fintech startup", "a game studio", "a logistics company",
              "a university research lab", "a law firm", "a retail chain", "a B2B SaaS company",
              "a government contractor", "a nonprofit", "a manufacturing plant", "a media company",
              "an insurance broker", "a telecom operator", "a biotech company", "a recruitment agency"]


# Natural-style batches (added after the first fine-tune showed a gap on short, colloquial hand-written cases).
# These rows only ever go to train or dev: the test split is frozen.
NATURAL_BATCHES = 2          # per positive category
NATURAL_SCENARIOS = 16       # pair scenarios per natural batch
NATURAL_EASY_BATCHES = 4
NATURAL_EASY_PER_BATCH = 40
XL_DEV_BATCHES = 2           # 8,000-character items for the dev split, to choose a long-text policy
