"""Pure, deterministic catalogs and planning for diverse personas.

This module is import-time side-effect free and depends only on the standard
library. It implements the catalog and population-aware planner described in
``aidocs/PERSONA_DIVERSITY_PROMPT_PLAN.md`` (Phase 1).
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgeBand:
    id: str
    low: int
    high: int
    target: float


@dataclass(frozen=True)
class EducationLevel:
    id: str
    label: str
    min_age: int | None = None


@dataclass(frozen=True)
class EducationOption:
    level_id: str
    text: str


@dataclass(frozen=True)
class EmploymentContext:
    id: str
    label: str
    min_age: int | None = None
    max_age: int | None = None


@dataclass(frozen=True)
class SectorOption:
    id: str
    label: str


@dataclass(frozen=True)
class OccupationOption:
    id: str
    label: str
    sector: str
    education_options: tuple[EducationOption, ...]
    allowed_contexts: tuple[str, ...]
    min_age: int | None = None
    max_age: int | None = None


@dataclass(frozen=True)
class TraitOption:
    id: str
    text: str
    axis: str
    limitation: bool = False


@dataclass(frozen=True)
class WritingStyleOption:
    id: str
    text: str
    family: str


@dataclass(frozen=True)
class InterestOption:
    id: str
    text: str
    domain: str


@dataclass(frozen=True)
class TrollModifier:
    id: str
    text: str


@dataclass(frozen=True)
class UsernameStyleOption:
    id: str
    text: str


@dataclass(frozen=True)
class ExistingUserSnapshot:
    persona_seed: Mapping[str, object] | None = None


@dataclass(frozen=True)
class PersonaAssignment:
    id: str
    age: int
    age_band_id: str
    occupation_id: str
    occupation: str
    occupation_sector: str
    employment_context_id: str
    employment_context: str
    education_level_id: str
    education: str
    trait_ids: tuple[str, ...]
    traits: tuple[str, ...]
    writing_style_id: str
    writing_style: str
    interest_seeds: tuple[str, ...]
    troll_modifier_id: str | None
    troll_modifier: str | None
    username_style: str


AGE_BANDS = (
    AgeBand("age.18_24", 18, 24, 0.15),
    AgeBand("age.25_34", 25, 34, 0.20),
    AgeBand("age.35_44", 35, 44, 0.20),
    AgeBand("age.45_54", 45, 54, 0.17),
    AgeBand("age.55_64", 55, 64, 0.15),
    AgeBand("age.65_75", 65, 75, 0.13),
)
EDUCATION_LEVELS = (
    EducationLevel("education.secondary_or_less", "Secondary education or less"),
    EducationLevel("education.high_school_or_ged", "High school diploma or GED"),
    EducationLevel("education.trade_or_vocational", "Trade or vocational certificate"),
    EducationLevel("education.current_student", "Currently a student"),
    EducationLevel("education.some_college", "Some college"),
    EducationLevel("education.associate", "Associate degree", 20),
    EducationLevel("education.bachelor", "Bachelor's degree", 22),
    EducationLevel(
        "education.graduate_or_professional", "Graduate or professional degree", 24
    ),
    EducationLevel(
        "education.self_taught_or_employer_trained", "Self-taught or employer-trained"
    ),
)
EDUCATION_LEVEL_TARGETS = {
    "education.secondary_or_less": 0.10,
    "education.high_school_or_ged": 0.16,
    "education.trade_or_vocational": 0.12,
    "education.current_student": 0.08,
    "education.some_college": 0.13,
    "education.associate": 0.12,
    "education.bachelor": 0.14,
    "education.graduate_or_professional": 0.08,
    "education.self_taught_or_employer_trained": 0.07,
}
EMPLOYMENT_CONTEXTS = (
    EmploymentContext("context.full_time", "full-time"),
    EmploymentContext("context.part_time", "part-time"),
    EmploymentContext("context.self_employed", "self-employed"),
    EmploymentContext("context.seasonal", "seasonal"),
    EmploymentContext("context.multiple_jobs", "working multiple jobs"),
    EmploymentContext("context.apprentice", "apprentice", max_age=45),
    EmploymentContext("context.current_student", "current student"),
    EmploymentContext("context.caregiver", "full-time caregiver"),
    EmploymentContext(
        "context.stay_at_home_parent", "stay-at-home parent", min_age=20, max_age=57
    ),
    EmploymentContext("context.between_jobs", "between jobs"),
    EmploymentContext("context.retired", "retired", min_age=55),
)
CONTEXT_BASE_WEIGHTS = {
    "context.full_time": 26,
    "context.part_time": 14,
    "context.self_employed": 9,
    "context.multiple_jobs": 8,
    "context.retired": 10,
    "context.between_jobs": 7,
    "context.current_student": 7,
    "context.seasonal": 5,
    "context.caregiver": 5,
    "context.stay_at_home_parent": 5,
    "context.apprentice": 4,
}

#: Age-band multipliers applied to context base weights: employment-context
#: prevalence shifts strongly with age (students cluster youngest, retirement
#: oldest, self-employment rises with age). A missing entry means 1.0; a 0.0
#: entry removes the context from that band entirely.
CONTEXT_BAND_WEIGHT_MULTIPLIERS: dict[str, dict[str, float]] = {
    "age.18_24": {
        "context.full_time": 0.85,
        "context.self_employed": 0.35,
        "context.seasonal": 1.30,
        "context.multiple_jobs": 1.20,
        "context.apprentice": 1.60,
        "context.current_student": 22.0,
        "context.caregiver": 0.15,
        "context.stay_at_home_parent": 0.15,
        "context.between_jobs": 1.20,
        "context.retired": 0.0,
    },
    "age.25_34": {
        "context.full_time": 1.10,
        "context.self_employed": 0.80,
        "context.apprentice": 0.80,
        "context.current_student": 0.25,
        "context.caregiver": 1.30,
        "context.stay_at_home_parent": 1.40,
        "context.retired": 0.0,
    },
    "age.35_44": {
        "context.full_time": 1.15,
        "context.self_employed": 1.10,
        "context.seasonal": 0.80,
        "context.multiple_jobs": 1.00,
        "context.apprentice": 0.25,
        "context.current_student": 0.03,
        "context.caregiver": 1.50,
        "context.stay_at_home_parent": 1.50,
        "context.between_jobs": 0.90,
        "context.retired": 0.0,
    },
    "age.45_54": {
        "context.full_time": 1.10,
        "context.self_employed": 1.30,
        "context.seasonal": 0.70,
        "context.multiple_jobs": 0.80,
        "context.apprentice": 0.0,
        "context.current_student": 0.01,
        "context.caregiver": 1.20,
        "context.stay_at_home_parent": 1.10,
        "context.between_jobs": 0.90,
        "context.retired": 0.15,
    },
    "age.55_64": {
        "context.full_time": 0.80,
        "context.self_employed": 1.50,
        "context.seasonal": 0.60,
        "context.multiple_jobs": 0.50,
        "context.current_student": 0.02,
        "context.caregiver": 0.70,
        "context.stay_at_home_parent": 0.50,
        "context.between_jobs": 0.70,
        "context.retired": 0.55,
    },
    "age.65_75": {
        "context.full_time": 0.40,
        "context.part_time": 1.30,
        "context.self_employed": 1.50,
        "context.seasonal": 0.40,
        "context.multiple_jobs": 0.30,
        "context.current_student": 0.0,
        "context.caregiver": 0.30,
        "context.stay_at_home_parent": 0.15,
        "context.between_jobs": 0.50,
        "context.retired": 2.20,
    },
}
SECTORS = (
    SectorOption("sector.food_and_hospitality", "Food and hospitality"),
    SectorOption("sector.retail_and_personal_services", "Retail and personal services"),
    SectorOption("sector.skilled_trades_and_repair", "Skilled trades and repair"),
    SectorOption("sector.construction_and_utilities", "Construction and utilities"),
    SectorOption("sector.transport_and_logistics", "Transport and logistics"),
    SectorOption("sector.manufacturing", "Manufacturing"),
    SectorOption("sector.healthcare_support", "Healthcare support"),
    SectorOption("sector.healthcare_professional", "Healthcare professional"),
    SectorOption("sector.education_and_community", "Education and community"),
    SectorOption("sector.public_service_and_safety", "Public service and safety"),
    SectorOption("sector.office_customer_and_finance", "Office, customer, and finance"),
    SectorOption("sector.agriculture_and_environment", "Agriculture and environment"),
    SectorOption(
        "sector.science_technical_and_professional",
        "Science, technical, and professional",
    ),
    SectorOption("sector.creative_media_and_culture", "Creative, media, and culture"),
    SectorOption("sector.technology_and_digital", "Technology and digital"),
    SectorOption(
        "sector.independent_and_irregular_work", "Independent and irregular work"
    ),
)
SECTOR_LABELS = {item.id: item.label for item in SECTORS}

_CTX_CORE = (
    "context.full_time",
    "context.part_time",
    "context.multiple_jobs",
    "context.between_jobs",
    "context.caregiver",
    "context.stay_at_home_parent",
    "context.retired",
)
_CTX_STUDENT = _CTX_CORE + ("context.current_student",)
_CTX_TRADE = _CTX_CORE + ("context.apprentice",)
_CTX_SELF = _CTX_CORE + ("context.self_employed",)
_CTX_SEASONAL = _CTX_CORE + ("context.seasonal",)
_CTX_TRADE_STUDENT = _CTX_TRADE + ("context.current_student",)
_CTX_SELF_STUDENT = _CTX_SELF + ("context.current_student",)

# Concrete credential routes per occupation card. Texts are specific to
# the role (the plan rejects generic one-size-fits-all sector strings)
# and each text always maps to the same canonical level.
_EDUCATION_ROUTES: dict[str, tuple[tuple[str, str], ...]] = {
    "line cook": (
        ("education.high_school_or_ged", "High school diploma plus kitchen experience"),
        ("education.trade_or_vocational", "Culinary-school certificate"),
        (
            "education.self_taught_or_employer_trained",
            "Self-taught cook who never went to culinary school",
        ),
    ),
    "prep cook": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.trade_or_vocational", "Culinary-certificate program"),
    ),
    "restaurant server": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
    ),
    "bartender": (
        (
            "education.high_school_or_ged",
            "High school diploma plus state bartending certification",
        ),
        ("education.trade_or_vocational", "Bartending-school certificate"),
    ),
    "baker": (
        ("education.trade_or_vocational", "Baking-and-pastry certificate"),
        (
            "education.high_school_or_ged",
            "High school diploma plus bakery apprenticeship",
        ),
        (
            "education.self_taught_or_employer_trained",
            "Trained on the job in a family bakery",
        ),
    ),
    "butcher": (
        ("education.trade_or_vocational", "Meatcutting apprenticeship"),
        ("education.high_school_or_ged", "High school diploma plus shop training"),
    ),
    "cafeteria worker": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.secondary_or_less", "Secondary education"),
        ("education.trade_or_vocational", "Food-handler certificate"),
    ),
    "hotel front-desk clerk": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
    ),
    "hotel housekeeper": (
        ("education.secondary_or_less", "Secondary education"),
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
    ),
    "catering coordinator": (
        ("education.some_college", "Some college"),
        (
            "education.high_school_or_ged",
            "High school diploma plus catering experience",
        ),
        ("education.associate", "A.A. Hospitality Management"),
    ),
    "grocery clerk": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
    ),
    "cashier": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.secondary_or_less", "Secondary education"),
    ),
    "retail supervisor": (
        ("education.high_school_or_ged", "High school diploma plus retail experience"),
        ("education.some_college", "Some college"),
        ("education.associate", "A.A. Business Administration"),
    ),
    "barber": (
        ("education.trade_or_vocational", "State barber license after barber school"),
        (
            "education.high_school_or_ged",
            "High school diploma plus barbering apprenticeship",
        ),
    ),
    "hair stylist": (
        ("education.trade_or_vocational", "Cosmetology license from beauty school"),
        (
            "education.high_school_or_ged",
            "High school diploma plus state cosmetology exam",
        ),
    ),
    "nail technician": (
        ("education.trade_or_vocational", "Nail-technician certificate"),
        ("education.high_school_or_ged", "High school diploma"),
    ),
    "massage therapist": (
        (
            "education.trade_or_vocational",
            "Massage-therapy certificate (500 program hours)",
        ),
        ("education.some_college", "Some college plus massage certification"),
    ),
    "dog groomer": (
        ("education.trade_or_vocational", "Grooming-academy certificate"),
        ("education.self_taught_or_employer_trained", "Learned grooming on the job"),
        ("education.high_school_or_ged", "High school diploma"),
    ),
    "tattoo artist": (
        (
            "education.self_taught_or_employer_trained",
            "Apprenticeship under a licensed tattoo artist",
        ),
        ("education.some_college", "Art-school coursework"),
    ),
    "funeral attendant": (
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
        ("education.some_college", "Some college"),
    ),
    "electrician": (
        ("education.trade_or_vocational", "Union apprenticeship (IBEW)"),
        (
            "education.high_school_or_ged",
            "High school diploma plus trade-school electrical program",
        ),
        ("education.associate", "A.A.S. Electrical Technology"),
    ),
    "plumber": (
        ("education.trade_or_vocational", "Union plumbing apprenticeship"),
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
        ("education.associate", "A.A.S. Plumbing Technology"),
    ),
    "HVAC technician": (
        ("education.trade_or_vocational", "HVAC certificate from trade school"),
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
        ("education.associate", "A.A.S. HVAC Technology"),
    ),
    "welder": (
        ("education.trade_or_vocational", "Welding certificate"),
        ("education.high_school_or_ged", "High school diploma plus shop training"),
        (
            "education.self_taught_or_employer_trained",
            "Learned welding in a family fabrication shop",
        ),
    ),
    "carpenter": (
        ("education.trade_or_vocational", "Union apprenticeship"),
        ("education.trade_or_vocational", "Vocational carpentry certificate"),
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
    ),
    "auto mechanic": (
        ("education.trade_or_vocational", "Automotive-technology certificate"),
        (
            "education.self_taught_or_employer_trained",
            "Employer-trained with ASE certifications",
        ),
        ("education.high_school_or_ged", "High school diploma plus shop training"),
    ),
    "diesel mechanic": (
        ("education.trade_or_vocational", "Diesel-technology certificate"),
        ("education.high_school_or_ged", "High school diploma plus shop training"),
        (
            "education.self_taught_or_employer_trained",
            "Employer-trained in a truck fleet shop",
        ),
    ),
    "appliance-repair technician": (
        ("education.trade_or_vocational", "Appliance-repair training program"),
        (
            "education.self_taught_or_employer_trained",
            "Manufacturer-trained service technician",
        ),
    ),
    "locksmith": (
        ("education.trade_or_vocational", "Locksmithing course and certification"),
        (
            "education.self_taught_or_employer_trained",
            "Learned locksmithing in a family shop",
        ),
    ),
    "bicycle mechanic": (
        (
            "education.self_taught_or_employer_trained",
            "Trained on the job at a bike shop",
        ),
        ("education.trade_or_vocational", "Bicycle-mechanics certificate"),
    ),
    "construction laborer": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.secondary_or_less", "Secondary education"),
        (
            "education.self_taught_or_employer_trained",
            "Learned construction from family jobsites",
        ),
    ),
    "heavy-equipment operator": (
        ("education.trade_or_vocational", "Heavy-equipment operator certificate"),
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
    ),
    "roofer": (
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
        ("education.trade_or_vocational", "Roofing apprenticeship"),
        (
            "education.self_taught_or_employer_trained",
            "Learned roofing from a family crew",
        ),
    ),
    "survey technician": (
        ("education.associate", "A.A.S. Surveying Technology"),
        ("education.high_school_or_ged", "High school diploma plus field training"),
    ),
    "electrical lineworker": (
        ("education.trade_or_vocational", "Lineworker training program"),
        (
            "education.high_school_or_ged",
            "High school diploma plus CDL and climbing certification",
        ),
    ),
    "water-treatment operator": (
        (
            "education.high_school_or_ged",
            "High school diploma plus state operator certification",
        ),
        ("education.associate", "A.A.S. Environmental Technology"),
    ),
    "solar installer": (
        ("education.trade_or_vocational", "Solar-installation certificate"),
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
        ("education.self_taught_or_employer_trained", "Employer-trained installer"),
    ),
    "building inspector": (
        ("education.trade_or_vocational", "ICC certification plus trade background"),
        ("education.associate", "A.A.S. Building Inspection Technology"),
        (
            "education.high_school_or_ged",
            "High school diploma plus construction experience",
        ),
    ),
    "utility-meter technician": (
        ("education.trade_or_vocational", "Utility-technician training program"),
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
    ),
    "arborist": (
        ("education.trade_or_vocational", "ISA Arborist certification"),
        ("education.associate", "A.A.S. Urban Forestry"),
        (
            "education.high_school_or_ged",
            "High school diploma plus climbing experience",
        ),
    ),
    "city-bus driver": (
        ("education.trade_or_vocational", "Commercial-driving training program"),
        (
            "education.high_school_or_ged",
            "High school diploma plus CDL with passenger endorsement",
        ),
    ),
    "long-haul truck driver": (
        ("education.trade_or_vocational", "CDL training program"),
        ("education.high_school_or_ged", "High school diploma plus CDL"),
        (
            "education.self_taught_or_employer_trained",
            "Learned driving in a family trucking business",
        ),
    ),
    "delivery courier": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.secondary_or_less", "Secondary education"),
        ("education.some_college", "Some college"),
    ),
    "warehouse picker": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.secondary_or_less", "Secondary education"),
        ("education.some_college", "Some college"),
    ),
    "forklift operator": (
        (
            "education.high_school_or_ged",
            "High school diploma plus forklift certification",
        ),
        ("education.secondary_or_less", "Secondary education"),
        ("education.self_taught_or_employer_trained", "Certified on the job"),
    ),
    "logistics dispatcher": (
        ("education.some_college", "Some college"),
        ("education.associate", "A.A.S. Supply-Chain Management"),
        (
            "education.high_school_or_ged",
            "High school diploma plus dispatch experience",
        ),
    ),
    "train conductor": (
        (
            "education.high_school_or_ged",
            "High school diploma plus railroad training program",
        ),
        ("education.some_college", "Some college"),
    ),
    "deckhand": (
        ("education.high_school_or_ged", "High school diploma"),
        (
            "education.trade_or_vocational",
            "Merchant Mariner Credential after maritime training",
        ),
        ("education.secondary_or_less", "Secondary education"),
    ),
    "baggage handler": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.secondary_or_less", "Secondary education"),
        ("education.some_college", "Some college"),
    ),
    "postal carrier": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
    ),
    "assembler": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.secondary_or_less", "Secondary education"),
        ("education.self_taught_or_employer_trained", "Trained on the assembly line"),
    ),
    "machinist": (
        ("education.trade_or_vocational", "Machinist apprenticeship"),
        ("education.associate", "A.A.S. Machine Tool Technology"),
        ("education.high_school_or_ged", "High school diploma plus shop training"),
    ),
    "CNC operator": (
        ("education.trade_or_vocational", "CNC certificate program"),
        ("education.high_school_or_ged", "High school diploma plus shop training"),
        ("education.self_taught_or_employer_trained", "Employer-trained CNC operator"),
    ),
    "quality inspector": (
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
        ("education.trade_or_vocational", "Quality-inspection certificate"),
        ("education.some_college", "Some college"),
    ),
    "packaging operator": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.secondary_or_less", "Secondary education"),
        ("education.some_college", "Some college"),
    ),
    "textile-machine operator": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.secondary_or_less", "Secondary education"),
        (
            "education.self_taught_or_employer_trained",
            "Trained on the job in a textile mill",
        ),
    ),
    "food-plant worker": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.secondary_or_less", "Secondary education"),
        (
            "education.self_taught_or_employer_trained",
            "Trained on the job in food processing",
        ),
    ),
    "print-shop operator": (
        ("education.trade_or_vocational", "Printing-technology certificate"),
        ("education.high_school_or_ged", "High school diploma plus pressroom training"),
        ("education.associate", "A.A.S. Printing Technology"),
    ),
    "maintenance mechanic": (
        ("education.trade_or_vocational", "Industrial-maintenance certificate"),
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
        (
            "education.self_taught_or_employer_trained",
            "Employer-trained maintenance mechanic",
        ),
    ),
    "production supervisor": (
        ("education.some_college", "Some college"),
        ("education.associate", "A.A.S. Manufacturing Technology"),
        (
            "education.high_school_or_ged",
            "High school diploma plus fifteen years on the line",
        ),
    ),
    "nursing assistant": (
        ("education.trade_or_vocational", "State CNA certificate"),
        ("education.some_college", "Some college (nursing prerequisites)"),
    ),
    "home-health aide": (
        ("education.trade_or_vocational", "Home-health-aide certificate"),
        ("education.high_school_or_ged", "High school diploma"),
        ("education.secondary_or_less", "Secondary education"),
    ),
    "medical assistant": (
        ("education.trade_or_vocational", "Medical-assistant certificate"),
        ("education.associate", "A.A.S. Medical Assisting"),
    ),
    "dental assistant": (
        ("education.trade_or_vocational", "Dental-assisting certificate"),
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
    ),
    "pharmacy technician": (
        ("education.trade_or_vocational", "Pharmacy-technician certificate (PTCB)"),
        ("education.associate", "A.A.S. Pharmacy Technology"),
    ),
    "phlebotomist": (
        ("education.trade_or_vocational", "Phlebotomy certificate"),
        ("education.high_school_or_ged", "High school diploma"),
    ),
    "respiratory therapist": (
        ("education.associate", "A.A.S. Respiratory Care"),
        ("education.bachelor", "B.S. Respiratory Care"),
    ),
    "radiologic technologist": (
        ("education.associate", "A.A.S. Radiography"),
        ("education.bachelor", "B.S. Radiologic Sciences"),
    ),
    "surgical technician": (
        ("education.trade_or_vocational", "Surgical-technologist certificate"),
        ("education.associate", "A.A.S. Surgical Technology"),
    ),
    "EMT": (
        ("education.trade_or_vocational", "EMT certification"),
        ("education.some_college", "Some college (paramedic prerequisites)"),
    ),
    "registered nurse": (
        ("education.associate", "A.D.N. Nursing"),
        ("education.bachelor", "B.S.N."),
    ),
    "dental hygienist": (
        ("education.associate", "A.A.S. Dental Hygiene"),
        ("education.bachelor", "B.S. Dental Hygiene"),
    ),
    "physical therapist": (
        ("education.graduate_or_professional", "D.P.T."),
        ("education.graduate_or_professional", "M.P.T. from an older program"),
    ),
    "occupational therapist": (
        ("education.graduate_or_professional", "M.O.T."),
        ("education.graduate_or_professional", "O.T.D."),
    ),
    "speech-language pathologist": (
        ("education.graduate_or_professional", "M.A. Speech-Language Pathology"),
        (
            "education.graduate_or_professional",
            "M.S. Communication Sciences and Disorders",
        ),
    ),
    "paramedic": (
        ("education.trade_or_vocational", "Paramedic certificate program"),
        ("education.associate", "A.A.S. Paramedic Technology"),
    ),
    "mental-health counselor": (
        (
            "education.graduate_or_professional",
            "M.A. Clinical Mental-Health Counseling",
        ),
        ("education.graduate_or_professional", "M.S. Clinical Psychology"),
    ),
    "dietitian": (
        ("education.bachelor", "B.S. Dietetics plus R.D. internship"),
        ("education.graduate_or_professional", "M.S. Nutrition"),
    ),
    "optician": (
        ("education.trade_or_vocational", "Optician training plus ABO certification"),
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
        ("education.associate", "A.A.S. Ophthalmic Dispensing"),
    ),
    "veterinarian": (("education.graduate_or_professional", "D.V.M."),),
    "preschool teacher": (
        (
            "education.trade_or_vocational",
            "Child Development Associate (CDA) credential",
        ),
        ("education.associate", "A.A. Early-Childhood Education"),
        ("education.bachelor", "B.S. Early-Childhood Education"),
    ),
    "elementary-school teacher": (
        ("education.bachelor", "B.S. Elementary Education"),
        ("education.graduate_or_professional", "M.Ed. Curriculum and Instruction"),
    ),
    "high-school teacher": (
        ("education.bachelor", "B.S. Secondary Education"),
        ("education.bachelor", "B.A. English with teaching certificate"),
        ("education.graduate_or_professional", "M.Ed."),
    ),
    "special-education paraprofessional": (
        ("education.some_college", "Some college"),
        ("education.trade_or_vocational", "Para-educator certificate"),
        ("education.high_school_or_ged", "High school diploma"),
    ),
    "school custodian": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.secondary_or_less", "Secondary education"),
        (
            "education.self_taught_or_employer_trained",
            "Trained on the job by the district",
        ),
    ),
    "social worker": (
        ("education.bachelor", "B.S.W."),
        ("education.graduate_or_professional", "M.S.W."),
    ),
    "youth counselor": (
        ("education.bachelor", "B.A. Psychology"),
        ("education.some_college", "Some college"),
        ("education.trade_or_vocational", "Youth-work certificate"),
    ),
    "academic adviser": (
        ("education.bachelor", "B.A. Sociology"),
        ("education.graduate_or_professional", "M.Ed. Higher-Education Administration"),
    ),
    "translator": (
        ("education.bachelor", "B.A. Linguistics"),
        (
            "education.self_taught_or_employer_trained",
            "Self-taught working translator (heritage speaker)",
        ),
        ("education.some_college", "Some college"),
    ),
    "community organizer": (
        ("education.some_college", "Some college"),
        (
            "education.self_taught_or_employer_trained",
            "Trained organizer through campaign work",
        ),
        ("education.bachelor", "B.A. Political Science"),
    ),
    "firefighter": (
        ("education.trade_or_vocational", "Fire-academy certificate"),
        ("education.associate", "A.A.S. Fire Science"),
        ("education.some_college", "Some college (fire-science coursework)"),
    ),
    "police dispatcher": (
        (
            "education.high_school_or_ged",
            "High school diploma plus emergency-dispatch certification",
        ),
        ("education.some_college", "Some college"),
    ),
    "court clerk": (
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
        ("education.some_college", "Some college"),
        ("education.associate", "A.A. Criminal Justice"),
    ),
    "correctional officer": (
        ("education.high_school_or_ged", "High school diploma plus academy training"),
        ("education.some_college", "Some college (criminal-justice coursework)"),
    ),
    "public-health inspector": (
        ("education.bachelor", "B.S. Public Health"),
        ("education.associate", "A.A.S. Environmental Health"),
        ("education.graduate_or_professional", "M.P.H."),
    ),
    "sanitation inspector": (
        ("education.associate", "A.A.S. Environmental Technology"),
        ("education.high_school_or_ged", "High school diploma plus field training"),
    ),
    "emergency manager": (
        ("education.bachelor", "B.S. Emergency Management"),
        ("education.graduate_or_professional", "M.P.A. Emergency Management"),
    ),
    "postal clerk": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
    ),
    "park ranger": (
        ("education.associate", "A.A.S. Park Management"),
        ("education.bachelor", "B.S. Natural Resources"),
        (
            "education.high_school_or_ged",
            "High school diploma plus seasonal ranger experience",
        ),
    ),
    "911 operator": (
        (
            "education.high_school_or_ged",
            "High school diploma plus emergency-dispatch certification",
        ),
        ("education.some_college", "Some college"),
    ),
    "receptionist": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
    ),
    "payroll clerk": (
        ("education.high_school_or_ged", "High school diploma plus payroll experience"),
        ("education.some_college", "Some college"),
        (
            "education.self_taught_or_employer_trained",
            "Employer-trained payroll specialist",
        ),
    ),
    "claims processor": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
        (
            "education.self_taught_or_employer_trained",
            "Employer-trained claims processor",
        ),
    ),
    "customer-support representative": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
    ),
    "legal assistant": (
        ("education.trade_or_vocational", "Paralegal certificate"),
        ("education.associate", "A.A.S. Paralegal Studies"),
        ("education.bachelor", "B.A. Legal Studies"),
    ),
    "bookkeeper": (
        (
            "education.self_taught_or_employer_trained",
            "Employer-trained after high school",
        ),
        ("education.associate", "A.A. Accounting"),
        ("education.bachelor", "B.S. Accounting"),
    ),
    "medical scheduler": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
        (
            "education.self_taught_or_employer_trained",
            "Employer-trained on scheduling systems",
        ),
    ),
    "records clerk": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
    ),
    "HR coordinator": (
        ("education.some_college", "Some college"),
        ("education.associate", "A.A. Business Administration"),
        ("education.bachelor", "B.A. Human Resources"),
    ),
    "loan processor": (
        ("education.some_college", "Some college"),
        (
            "education.self_taught_or_employer_trained",
            "Employer-trained under a senior underwriter",
        ),
        ("education.bachelor", "B.S. Business Administration"),
    ),
    "farmhand": (
        ("education.secondary_or_less", "Secondary education"),
        ("education.high_school_or_ged", "High school diploma"),
        ("education.self_taught_or_employer_trained", "Grew up on the family farm"),
    ),
    "dairy worker": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.secondary_or_less", "Secondary education"),
        ("education.self_taught_or_employer_trained", "Raised on a dairy farm"),
    ),
    "greenhouse grower": (
        ("education.trade_or_vocational", "Horticulture certificate"),
        ("education.associate", "A.A.S. Greenhouse Management"),
        (
            "education.self_taught_or_employer_trained",
            "Learned growing in a family nursery",
        ),
    ),
    "landscaper": (
        ("education.trade_or_vocational", "Landscape-technician certificate"),
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
        (
            "education.self_taught_or_employer_trained",
            "Learned landscaping from a family crew",
        ),
    ),
    "groundskeeper": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.secondary_or_less", "Secondary education"),
        (
            "education.self_taught_or_employer_trained",
            "Learned grounds care on the job",
        ),
    ),
    "forestry technician": (
        ("education.associate", "A.A.S. Forestry Technology"),
        ("education.bachelor", "B.S. Forestry"),
        ("education.high_school_or_ged", "High school diploma plus field experience"),
    ),
    "fisheries technician": (
        ("education.associate", "A.A.S. Fisheries Technology"),
        ("education.bachelor", "B.S. Biology"),
        ("education.high_school_or_ged", "High school diploma plus field experience"),
    ),
    "recycling sorter": (
        ("education.secondary_or_less", "Secondary education"),
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
    ),
    "waste collector": (
        ("education.secondary_or_less", "Secondary education"),
        ("education.high_school_or_ged", "High school diploma"),
    ),
    "pest-control technician": (
        ("education.trade_or_vocational", "State pesticide-applicator license"),
        ("education.high_school_or_ged", "High school diploma"),
        ("education.self_taught_or_employer_trained", "Employer-trained applicator"),
    ),
    "laboratory technician": (
        ("education.associate", "A.A.S. Laboratory Technology"),
        ("education.bachelor", "B.S. Biology"),
        ("education.trade_or_vocational", "Laboratory-technician certificate"),
    ),
    "GIS technician": (
        ("education.trade_or_vocational", "GIS certificate"),
        ("education.associate", "A.A.S. Geospatial Technology"),
        ("education.bachelor", "B.S. Geography"),
    ),
    "civil-engineering technician": (
        ("education.trade_or_vocational", "Civil-technology certificate"),
        ("education.associate", "A.A.S. Civil Engineering Technology"),
        ("education.some_college", "Some college toward an engineering degree"),
    ),
    "accountant": (
        ("education.associate", "A.A. Accounting"),
        ("education.bachelor", "B.S. Accounting"),
        ("education.graduate_or_professional", "M.S. Accounting"),
    ),
    "paralegal": (
        ("education.trade_or_vocational", "Paralegal certificate"),
        ("education.associate", "A.A.S. Paralegal Studies"),
        ("education.bachelor", "B.A. Legal Studies"),
    ),
    "insurance underwriter": (
        ("education.some_college", "Some college"),
        ("education.self_taught_or_employer_trained", "Employer-trained underwriter"),
        ("education.bachelor", "B.S. Business (risk-management concentration)"),
    ),
    "urban planner": (
        ("education.bachelor", "B.S. Urban Studies"),
        ("education.graduate_or_professional", "M.U.P. Urban Planning"),
    ),
    "chemist": (
        ("education.bachelor", "B.S. Chemistry"),
        ("education.graduate_or_professional", "Ph.D. Chemistry"),
    ),
    "statistician": (
        ("education.bachelor", "B.S. Statistics"),
        ("education.graduate_or_professional", "M.S. Statistics"),
    ),
    "land surveyor": (
        ("education.associate", "A.A.S. Surveying Technology"),
        ("education.bachelor", "B.S. Geomatics"),
        ("education.trade_or_vocational", "State P.S. license after apprenticeship"),
    ),
    "graphic designer": (
        ("education.bachelor", "B.F.A. Graphic Design"),
        (
            "education.self_taught_or_employer_trained",
            "Self-taught designer with a strong portfolio",
        ),
        ("education.trade_or_vocational", "Design-certificate program"),
    ),
    "photographer": (
        (
            "education.self_taught_or_employer_trained",
            "Self-taught through years of shoots",
        ),
        ("education.some_college", "Some college (art coursework)"),
        ("education.trade_or_vocational", "Photography certificate"),
    ),
    "audio technician": (
        ("education.trade_or_vocational", "Audio-engineering certificate"),
        ("education.self_taught_or_employer_trained", "Learned live sound in clubs"),
        ("education.associate", "A.A.S. Audio Production"),
    ),
    "stagehand": (
        ("education.trade_or_vocational", "Stagehand apprenticeship (IATSE)"),
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
    ),
    "copy editor": (
        ("education.bachelor", "B.A. English"),
        ("education.trade_or_vocational", "Copyediting certificate"),
        ("education.some_college", "Some college"),
    ),
    "sign painter": (
        (
            "education.self_taught_or_employer_trained",
            "Learned hand-lettering as an apprentice",
        ),
        ("education.trade_or_vocational", "Sign-painting apprenticeship"),
        ("education.high_school_or_ged", "High school diploma"),
    ),
    "florist": (
        ("education.trade_or_vocational", "Floral-design certificate"),
        (
            "education.self_taught_or_employer_trained",
            "Learned design in a flower shop",
        ),
        ("education.high_school_or_ged", "High school diploma"),
    ),
    "tailor": (
        ("education.trade_or_vocational", "Tailoring apprenticeship"),
        (
            "education.self_taught_or_employer_trained",
            "Learned from a family tailoring business",
        ),
        ("education.high_school_or_ged", "High school diploma"),
    ),
    "community-radio producer": (
        ("education.some_college", "Some college"),
        (
            "education.self_taught_or_employer_trained",
            "Trained at the station as a volunteer",
        ),
    ),
    "wedding DJ": (
        (
            "education.self_taught_or_employer_trained",
            "Self-taught DJ with a mobile rig",
        ),
        ("education.trade_or_vocational", "Audio and event-production certificate"),
        ("education.some_college", "Some college"),
    ),
    "help-desk technician": (
        ("education.trade_or_vocational", "IT-support certificate (CompTIA A+)"),
        ("education.associate", "A.A.S. Information Technology"),
        (
            "education.self_taught_or_employer_trained",
            "Self-taught, started on family computers",
        ),
    ),
    "network technician": (
        ("education.trade_or_vocational", "Network certificate (CCNA)"),
        ("education.associate", "A.A.S. Network Administration"),
        (
            "education.self_taught_or_employer_trained",
            "Self-taught home-lab network engineer",
        ),
    ),
    "systems administrator": (
        (
            "education.self_taught_or_employer_trained",
            "Self-taught Linux administrator",
        ),
        ("education.associate", "A.A.S. Information Technology"),
        ("education.bachelor", "B.S. Information Systems"),
    ),
    "web developer": (
        (
            "education.self_taught_or_employer_trained",
            "Self-taught through open-source projects",
        ),
        ("education.trade_or_vocational", "Web-development certificate"),
        ("education.bachelor", "B.S. Information Systems"),
    ),
    "software engineer": (
        ("education.bachelor", "B.S. Computer Science"),
        ("education.trade_or_vocational", "Coding-bootcamp certificate"),
        ("education.graduate_or_professional", "M.S. Computer Science"),
    ),
    "QA analyst": (
        ("education.trade_or_vocational", "Coding-bootcamp certificate"),
        ("education.self_taught_or_employer_trained", "Self-taught tester"),
        ("education.bachelor", "B.S. Computer Science"),
    ),
    "data analyst": (
        ("education.trade_or_vocational", "Data-analytics certificate"),
        ("education.self_taught_or_employer_trained", "Self-taught SQL analyst"),
        ("education.bachelor", "B.S. Statistics"),
    ),
    "cybersecurity analyst": (
        ("education.trade_or_vocational", "Security certificate (Security+)"),
        ("education.bachelor", "B.S. Cybersecurity"),
        (
            "education.self_taught_or_employer_trained",
            "Self-taught from CTF competitions",
        ),
    ),
    "UX researcher": (
        ("education.bachelor", "B.S. Psychology"),
        ("education.graduate_or_professional", "M.S. Human-Computer Interaction"),
        ("education.some_college", "Some college"),
    ),
    "IT trainer": (
        ("education.trade_or_vocational", "Corporate-trainer certificate"),
        ("education.bachelor", "B.A. Communication"),
        ("education.associate", "A.A. Business Administration"),
    ),
    "rideshare driver": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
    ),
    "market vendor": (
        ("education.secondary_or_less", "Secondary education"),
        ("education.high_school_or_ged", "High school diploma"),
        (
            "education.self_taught_or_employer_trained",
            "Grew up working family market stalls",
        ),
    ),
    "house cleaner": (
        ("education.secondary_or_less", "Secondary education"),
        ("education.high_school_or_ged", "High school diploma"),
        (
            "education.self_taught_or_employer_trained",
            "Started a cleaning route with no formal training",
        ),
    ),
    "handyman": (
        (
            "education.self_taught_or_employer_trained",
            "Self-taught handyman with twenty years of fixes",
        ),
        (
            "education.high_school_or_ged",
            "High school diploma plus on-the-job training",
        ),
        ("education.trade_or_vocational", "Home-repair certificate course"),
    ),
    "pet sitter": (
        ("education.high_school_or_ged", "High school diploma"),
        (
            "education.self_taught_or_employer_trained",
            "Built a pet-sitting business from word of mouth",
        ),
    ),
    "seasonal resort worker": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
    ),
    "childcare provider": (
        (
            "education.trade_or_vocational",
            "Child Development Associate (CDA) credential",
        ),
        ("education.high_school_or_ged", "High school diploma"),
        (
            "education.self_taught_or_employer_trained",
            "Watched neighborhood kids since high school",
        ),
    ),
    "online reseller": (
        ("education.self_taught_or_employer_trained", "Self-taught reseller"),
        ("education.high_school_or_ged", "High school diploma"),
    ),
    "mobile notary": (
        ("education.high_school_or_ged", "High school diploma plus notary commission"),
        (
            "education.self_taught_or_employer_trained",
            "Commissioned notary with loan-signing training",
        ),
        ("education.some_college", "Some college"),
    ),
    "food-delivery courier": (
        ("education.high_school_or_ged", "High school diploma"),
        ("education.some_college", "Some college"),
    ),
}

# Roles plausibly held by a current student (service/retail/labor/office/
# creative/support jobs). Only these cards offer the current-student
# education level and employment context; licensed, credentialed, and
# professional roles never do.
_STUDENT_PLAUSIBLE = frozenset(
    (
        "line cook",
        "prep cook",
        "restaurant server",
        "bartender",
        "baker",
        "cafeteria worker",
        "hotel front-desk clerk",
        "catering coordinator",
        "grocery clerk",
        "cashier",
        "bicycle mechanic",
        "construction laborer",
        "delivery courier",
        "warehouse picker",
        "baggage handler",
        "assembler",
        "packaging operator",
        "food-plant worker",
        "nursing assistant",
        "medical assistant",
        "pharmacy technician",
        "phlebotomist",
        "special-education paraprofessional",
        "school custodian",
        "translator",
        "community organizer",
        "receptionist",
        "customer-support representative",
        "records clerk",
        "farmhand",
        "photographer",
        "stagehand",
        "community-radio producer",
        "help-desk technician",
        "web developer",
        "QA analyst",
        "data analyst",
        "rideshare driver",
        "market vendor",
        "pet sitter",
        "seasonal resort worker",
        "childcare provider",
        "online reseller",
        "food-delivery courier",
    )
)


def _education_options(label: str, sector: str) -> tuple[EducationOption, ...]:
    """Concrete credential strings for one occupation card.

    Student-plausible roles additionally offer the current-student level,
    so the option list and the student employment context stay in sync
    through one membership check. An unknown label fails loudly at
    import time. The sector argument is intentionally unused here but
    kept so both card helpers read uniformly.
    """
    del sector
    routes = list(_EDUCATION_ROUTES[label])
    if label in _STUDENT_PLAUSIBLE:
        routes.append(("education.current_student", "Currently a student"))
    return tuple(EducationOption(level_id, text) for level_id, text in routes)


def _occupation_contexts(label: str, sector: str) -> tuple[str, ...]:
    """Employment contexts allowed for one occupation card."""
    student = label in _STUDENT_PLAUSIBLE
    self_employed = {
        "barber",
        "hair stylist",
        "nail technician",
        "massage therapist",
        "dog groomer",
        "tattoo artist",
        "photographer",
        "tailor",
        "florist",
        "wedding DJ",
        "rideshare driver",
        "market vendor",
        "house cleaner",
        "handyman",
        "pet sitter",
        "childcare provider",
        "online reseller",
        "mobile notary",
        "food-delivery courier",
        "locksmith",
    }
    seasonal = sector in (
        "sector.food_and_hospitality",
        "sector.agriculture_and_environment",
    ) or label in (
        "park ranger",
        "seasonal resort worker",
        "landscaper",
        "groundskeeper",
        "fisheries technician",
    )
    apprentice = sector in (
        "sector.skilled_trades_and_repair",
        "sector.construction_and_utilities",
        "sector.manufacturing",
    ) or label in ("line cook", "prep cook", "baker", "butcher")
    if student:
        if label in self_employed:
            return _CTX_SELF_STUDENT
        if apprentice:
            return _CTX_TRADE_STUDENT
        return _CTX_STUDENT
    if label in self_employed:
        return _CTX_SELF
    if apprentice:
        return _CTX_TRADE
    if seasonal:
        return _CTX_SEASONAL
    return _CTX_CORE


OCCUPATIONS = (
    OccupationOption(
        "occupation.line_cook",
        "line cook",
        "sector.food_and_hospitality",
        _education_options("line cook", "sector.food_and_hospitality"),
        _occupation_contexts("line cook", "sector.food_and_hospitality"),
        18,
    ),
    OccupationOption(
        "occupation.prep_cook",
        "prep cook",
        "sector.food_and_hospitality",
        _education_options("prep cook", "sector.food_and_hospitality"),
        _occupation_contexts("prep cook", "sector.food_and_hospitality"),
        18,
    ),
    OccupationOption(
        "occupation.restaurant_server",
        "restaurant server",
        "sector.food_and_hospitality",
        _education_options("restaurant server", "sector.food_and_hospitality"),
        _occupation_contexts("restaurant server", "sector.food_and_hospitality"),
        18,
    ),
    OccupationOption(
        "occupation.bartender",
        "bartender",
        "sector.food_and_hospitality",
        _education_options("bartender", "sector.food_and_hospitality"),
        _occupation_contexts("bartender", "sector.food_and_hospitality"),
        18,
    ),
    OccupationOption(
        "occupation.baker",
        "baker",
        "sector.food_and_hospitality",
        _education_options("baker", "sector.food_and_hospitality"),
        _occupation_contexts("baker", "sector.food_and_hospitality"),
        21,
    ),
    OccupationOption(
        "occupation.butcher",
        "butcher",
        "sector.food_and_hospitality",
        _education_options("butcher", "sector.food_and_hospitality"),
        _occupation_contexts("butcher", "sector.food_and_hospitality"),
        21,
    ),
    OccupationOption(
        "occupation.cafeteria_worker",
        "cafeteria worker",
        "sector.food_and_hospitality",
        _education_options("cafeteria worker", "sector.food_and_hospitality"),
        _occupation_contexts("cafeteria worker", "sector.food_and_hospitality"),
        18,
    ),
    OccupationOption(
        "occupation.hotel_front_desk_clerk",
        "hotel front-desk clerk",
        "sector.food_and_hospitality",
        _education_options("hotel front-desk clerk", "sector.food_and_hospitality"),
        _occupation_contexts("hotel front-desk clerk", "sector.food_and_hospitality"),
        18,
    ),
    OccupationOption(
        "occupation.hotel_housekeeper",
        "hotel housekeeper",
        "sector.food_and_hospitality",
        _education_options("hotel housekeeper", "sector.food_and_hospitality"),
        _occupation_contexts("hotel housekeeper", "sector.food_and_hospitality"),
        18,
    ),
    OccupationOption(
        "occupation.catering_coordinator",
        "catering coordinator",
        "sector.food_and_hospitality",
        _education_options("catering coordinator", "sector.food_and_hospitality"),
        _occupation_contexts("catering coordinator", "sector.food_and_hospitality"),
        18,
    ),
    OccupationOption(
        "occupation.grocery_clerk",
        "grocery clerk",
        "sector.retail_and_personal_services",
        _education_options("grocery clerk", "sector.retail_and_personal_services"),
        _occupation_contexts("grocery clerk", "sector.retail_and_personal_services"),
        18,
    ),
    OccupationOption(
        "occupation.cashier",
        "cashier",
        "sector.retail_and_personal_services",
        _education_options("cashier", "sector.retail_and_personal_services"),
        _occupation_contexts("cashier", "sector.retail_and_personal_services"),
        18,
    ),
    OccupationOption(
        "occupation.retail_supervisor",
        "retail supervisor",
        "sector.retail_and_personal_services",
        _education_options("retail supervisor", "sector.retail_and_personal_services"),
        _occupation_contexts(
            "retail supervisor", "sector.retail_and_personal_services"
        ),
        23,
    ),
    OccupationOption(
        "occupation.barber",
        "barber",
        "sector.retail_and_personal_services",
        _education_options("barber", "sector.retail_and_personal_services"),
        _occupation_contexts("barber", "sector.retail_and_personal_services"),
        18,
    ),
    OccupationOption(
        "occupation.hair_stylist",
        "hair stylist",
        "sector.retail_and_personal_services",
        _education_options("hair stylist", "sector.retail_and_personal_services"),
        _occupation_contexts("hair stylist", "sector.retail_and_personal_services"),
        18,
    ),
    OccupationOption(
        "occupation.nail_technician",
        "nail technician",
        "sector.retail_and_personal_services",
        _education_options("nail technician", "sector.retail_and_personal_services"),
        _occupation_contexts("nail technician", "sector.retail_and_personal_services"),
        18,
    ),
    OccupationOption(
        "occupation.massage_therapist",
        "massage therapist",
        "sector.retail_and_personal_services",
        _education_options("massage therapist", "sector.retail_and_personal_services"),
        _occupation_contexts(
            "massage therapist", "sector.retail_and_personal_services"
        ),
        18,
    ),
    OccupationOption(
        "occupation.dog_groomer",
        "dog groomer",
        "sector.retail_and_personal_services",
        _education_options("dog groomer", "sector.retail_and_personal_services"),
        _occupation_contexts("dog groomer", "sector.retail_and_personal_services"),
        18,
    ),
    OccupationOption(
        "occupation.tattoo_artist",
        "tattoo artist",
        "sector.retail_and_personal_services",
        _education_options("tattoo artist", "sector.retail_and_personal_services"),
        _occupation_contexts("tattoo artist", "sector.retail_and_personal_services"),
        18,
    ),
    OccupationOption(
        "occupation.funeral_attendant",
        "funeral attendant",
        "sector.retail_and_personal_services",
        _education_options("funeral attendant", "sector.retail_and_personal_services"),
        _occupation_contexts(
            "funeral attendant", "sector.retail_and_personal_services"
        ),
        18,
    ),
    OccupationOption(
        "occupation.electrician",
        "electrician",
        "sector.skilled_trades_and_repair",
        _education_options("electrician", "sector.skilled_trades_and_repair"),
        _occupation_contexts("electrician", "sector.skilled_trades_and_repair"),
        21,
    ),
    OccupationOption(
        "occupation.plumber",
        "plumber",
        "sector.skilled_trades_and_repair",
        _education_options("plumber", "sector.skilled_trades_and_repair"),
        _occupation_contexts("plumber", "sector.skilled_trades_and_repair"),
        21,
    ),
    OccupationOption(
        "occupation.hvac_technician",
        "HVAC technician",
        "sector.skilled_trades_and_repair",
        _education_options("HVAC technician", "sector.skilled_trades_and_repair"),
        _occupation_contexts("HVAC technician", "sector.skilled_trades_and_repair"),
        21,
    ),
    OccupationOption(
        "occupation.welder",
        "welder",
        "sector.skilled_trades_and_repair",
        _education_options("welder", "sector.skilled_trades_and_repair"),
        _occupation_contexts("welder", "sector.skilled_trades_and_repair"),
        18,
    ),
    OccupationOption(
        "occupation.carpenter",
        "carpenter",
        "sector.skilled_trades_and_repair",
        _education_options("carpenter", "sector.skilled_trades_and_repair"),
        _occupation_contexts("carpenter", "sector.skilled_trades_and_repair"),
        21,
    ),
    OccupationOption(
        "occupation.auto_mechanic",
        "auto mechanic",
        "sector.skilled_trades_and_repair",
        _education_options("auto mechanic", "sector.skilled_trades_and_repair"),
        _occupation_contexts("auto mechanic", "sector.skilled_trades_and_repair"),
        18,
    ),
    OccupationOption(
        "occupation.diesel_mechanic",
        "diesel mechanic",
        "sector.skilled_trades_and_repair",
        _education_options("diesel mechanic", "sector.skilled_trades_and_repair"),
        _occupation_contexts("diesel mechanic", "sector.skilled_trades_and_repair"),
        18,
    ),
    OccupationOption(
        "occupation.appliance_repair_technician",
        "appliance-repair technician",
        "sector.skilled_trades_and_repair",
        _education_options(
            "appliance-repair technician", "sector.skilled_trades_and_repair"
        ),
        _occupation_contexts(
            "appliance-repair technician", "sector.skilled_trades_and_repair"
        ),
        18,
    ),
    OccupationOption(
        "occupation.locksmith",
        "locksmith",
        "sector.skilled_trades_and_repair",
        _education_options("locksmith", "sector.skilled_trades_and_repair"),
        _occupation_contexts("locksmith", "sector.skilled_trades_and_repair"),
        19,
    ),
    OccupationOption(
        "occupation.bicycle_mechanic",
        "bicycle mechanic",
        "sector.skilled_trades_and_repair",
        _education_options("bicycle mechanic", "sector.skilled_trades_and_repair"),
        _occupation_contexts("bicycle mechanic", "sector.skilled_trades_and_repair"),
        18,
    ),
    OccupationOption(
        "occupation.construction_laborer",
        "construction laborer",
        "sector.construction_and_utilities",
        _education_options("construction laborer", "sector.construction_and_utilities"),
        _occupation_contexts(
            "construction laborer", "sector.construction_and_utilities"
        ),
        18,
    ),
    OccupationOption(
        "occupation.heavy_equipment_operator",
        "heavy-equipment operator",
        "sector.construction_and_utilities",
        _education_options(
            "heavy-equipment operator", "sector.construction_and_utilities"
        ),
        _occupation_contexts(
            "heavy-equipment operator", "sector.construction_and_utilities"
        ),
        19,
    ),
    OccupationOption(
        "occupation.roofer",
        "roofer",
        "sector.construction_and_utilities",
        _education_options("roofer", "sector.construction_and_utilities"),
        _occupation_contexts("roofer", "sector.construction_and_utilities"),
        18,
    ),
    OccupationOption(
        "occupation.survey_technician",
        "survey technician",
        "sector.construction_and_utilities",
        _education_options("survey technician", "sector.construction_and_utilities"),
        _occupation_contexts("survey technician", "sector.construction_and_utilities"),
        18,
    ),
    OccupationOption(
        "occupation.electrical_lineworker",
        "electrical lineworker",
        "sector.construction_and_utilities",
        _education_options(
            "electrical lineworker", "sector.construction_and_utilities"
        ),
        _occupation_contexts(
            "electrical lineworker", "sector.construction_and_utilities"
        ),
        21,
    ),
    OccupationOption(
        "occupation.water_treatment_operator",
        "water-treatment operator",
        "sector.construction_and_utilities",
        _education_options(
            "water-treatment operator", "sector.construction_and_utilities"
        ),
        _occupation_contexts(
            "water-treatment operator", "sector.construction_and_utilities"
        ),
        20,
    ),
    OccupationOption(
        "occupation.solar_installer",
        "solar installer",
        "sector.construction_and_utilities",
        _education_options("solar installer", "sector.construction_and_utilities"),
        _occupation_contexts("solar installer", "sector.construction_and_utilities"),
        18,
    ),
    OccupationOption(
        "occupation.building_inspector",
        "building inspector",
        "sector.construction_and_utilities",
        _education_options("building inspector", "sector.construction_and_utilities"),
        _occupation_contexts("building inspector", "sector.construction_and_utilities"),
        23,
    ),
    OccupationOption(
        "occupation.utility_meter_technician",
        "utility-meter technician",
        "sector.construction_and_utilities",
        _education_options(
            "utility-meter technician", "sector.construction_and_utilities"
        ),
        _occupation_contexts(
            "utility-meter technician", "sector.construction_and_utilities"
        ),
        18,
    ),
    OccupationOption(
        "occupation.arborist",
        "arborist",
        "sector.construction_and_utilities",
        _education_options("arborist", "sector.construction_and_utilities"),
        _occupation_contexts("arborist", "sector.construction_and_utilities"),
        19,
    ),
    OccupationOption(
        "occupation.city_bus_driver",
        "city-bus driver",
        "sector.transport_and_logistics",
        _education_options("city-bus driver", "sector.transport_and_logistics"),
        _occupation_contexts("city-bus driver", "sector.transport_and_logistics"),
        21,
    ),
    OccupationOption(
        "occupation.long_haul_truck_driver",
        "long-haul truck driver",
        "sector.transport_and_logistics",
        _education_options("long-haul truck driver", "sector.transport_and_logistics"),
        _occupation_contexts(
            "long-haul truck driver", "sector.transport_and_logistics"
        ),
        21,
    ),
    OccupationOption(
        "occupation.delivery_courier",
        "delivery courier",
        "sector.transport_and_logistics",
        _education_options("delivery courier", "sector.transport_and_logistics"),
        _occupation_contexts("delivery courier", "sector.transport_and_logistics"),
        18,
    ),
    OccupationOption(
        "occupation.warehouse_picker",
        "warehouse picker",
        "sector.transport_and_logistics",
        _education_options("warehouse picker", "sector.transport_and_logistics"),
        _occupation_contexts("warehouse picker", "sector.transport_and_logistics"),
        18,
    ),
    OccupationOption(
        "occupation.forklift_operator",
        "forklift operator",
        "sector.transport_and_logistics",
        _education_options("forklift operator", "sector.transport_and_logistics"),
        _occupation_contexts("forklift operator", "sector.transport_and_logistics"),
        18,
    ),
    OccupationOption(
        "occupation.logistics_dispatcher",
        "logistics dispatcher",
        "sector.transport_and_logistics",
        _education_options("logistics dispatcher", "sector.transport_and_logistics"),
        _occupation_contexts("logistics dispatcher", "sector.transport_and_logistics"),
        18,
    ),
    OccupationOption(
        "occupation.train_conductor",
        "train conductor",
        "sector.transport_and_logistics",
        _education_options("train conductor", "sector.transport_and_logistics"),
        _occupation_contexts("train conductor", "sector.transport_and_logistics"),
        21,
    ),
    OccupationOption(
        "occupation.deckhand",
        "deckhand",
        "sector.transport_and_logistics",
        _education_options("deckhand", "sector.transport_and_logistics"),
        _occupation_contexts("deckhand", "sector.transport_and_logistics"),
        18,
    ),
    OccupationOption(
        "occupation.baggage_handler",
        "baggage handler",
        "sector.transport_and_logistics",
        _education_options("baggage handler", "sector.transport_and_logistics"),
        _occupation_contexts("baggage handler", "sector.transport_and_logistics"),
        18,
    ),
    OccupationOption(
        "occupation.postal_carrier",
        "postal carrier",
        "sector.transport_and_logistics",
        _education_options("postal carrier", "sector.transport_and_logistics"),
        _occupation_contexts("postal carrier", "sector.transport_and_logistics"),
        18,
    ),
    OccupationOption(
        "occupation.assembler",
        "assembler",
        "sector.manufacturing",
        _education_options("assembler", "sector.manufacturing"),
        _occupation_contexts("assembler", "sector.manufacturing"),
        18,
    ),
    OccupationOption(
        "occupation.machinist",
        "machinist",
        "sector.manufacturing",
        _education_options("machinist", "sector.manufacturing"),
        _occupation_contexts("machinist", "sector.manufacturing"),
        21,
    ),
    OccupationOption(
        "occupation.cnc_operator",
        "CNC operator",
        "sector.manufacturing",
        _education_options("CNC operator", "sector.manufacturing"),
        _occupation_contexts("CNC operator", "sector.manufacturing"),
        18,
    ),
    OccupationOption(
        "occupation.quality_inspector",
        "quality inspector",
        "sector.manufacturing",
        _education_options("quality inspector", "sector.manufacturing"),
        _occupation_contexts("quality inspector", "sector.manufacturing"),
        18,
    ),
    OccupationOption(
        "occupation.packaging_operator",
        "packaging operator",
        "sector.manufacturing",
        _education_options("packaging operator", "sector.manufacturing"),
        _occupation_contexts("packaging operator", "sector.manufacturing"),
        18,
    ),
    OccupationOption(
        "occupation.textile_machine_operator",
        "textile-machine operator",
        "sector.manufacturing",
        _education_options("textile-machine operator", "sector.manufacturing"),
        _occupation_contexts("textile-machine operator", "sector.manufacturing"),
        18,
    ),
    OccupationOption(
        "occupation.food_plant_worker",
        "food-plant worker",
        "sector.manufacturing",
        _education_options("food-plant worker", "sector.manufacturing"),
        _occupation_contexts("food-plant worker", "sector.manufacturing"),
        18,
    ),
    OccupationOption(
        "occupation.print_shop_operator",
        "print-shop operator",
        "sector.manufacturing",
        _education_options("print-shop operator", "sector.manufacturing"),
        _occupation_contexts("print-shop operator", "sector.manufacturing"),
        18,
    ),
    OccupationOption(
        "occupation.maintenance_mechanic",
        "maintenance mechanic",
        "sector.manufacturing",
        _education_options("maintenance mechanic", "sector.manufacturing"),
        _occupation_contexts("maintenance mechanic", "sector.manufacturing"),
        20,
    ),
    OccupationOption(
        "occupation.production_supervisor",
        "production supervisor",
        "sector.manufacturing",
        _education_options("production supervisor", "sector.manufacturing"),
        _occupation_contexts("production supervisor", "sector.manufacturing"),
        23,
    ),
    OccupationOption(
        "occupation.nursing_assistant",
        "nursing assistant",
        "sector.healthcare_support",
        _education_options("nursing assistant", "sector.healthcare_support"),
        _occupation_contexts("nursing assistant", "sector.healthcare_support"),
        18,
    ),
    OccupationOption(
        "occupation.home_health_aide",
        "home-health aide",
        "sector.healthcare_support",
        _education_options("home-health aide", "sector.healthcare_support"),
        _occupation_contexts("home-health aide", "sector.healthcare_support"),
        18,
    ),
    OccupationOption(
        "occupation.medical_assistant",
        "medical assistant",
        "sector.healthcare_support",
        _education_options("medical assistant", "sector.healthcare_support"),
        _occupation_contexts("medical assistant", "sector.healthcare_support"),
        18,
    ),
    OccupationOption(
        "occupation.dental_assistant",
        "dental assistant",
        "sector.healthcare_support",
        _education_options("dental assistant", "sector.healthcare_support"),
        _occupation_contexts("dental assistant", "sector.healthcare_support"),
        18,
    ),
    OccupationOption(
        "occupation.pharmacy_technician",
        "pharmacy technician",
        "sector.healthcare_support",
        _education_options("pharmacy technician", "sector.healthcare_support"),
        _occupation_contexts("pharmacy technician", "sector.healthcare_support"),
        18,
    ),
    OccupationOption(
        "occupation.phlebotomist",
        "phlebotomist",
        "sector.healthcare_support",
        _education_options("phlebotomist", "sector.healthcare_support"),
        _occupation_contexts("phlebotomist", "sector.healthcare_support"),
        18,
    ),
    OccupationOption(
        "occupation.respiratory_therapist",
        "respiratory therapist",
        "sector.healthcare_support",
        _education_options("respiratory therapist", "sector.healthcare_support"),
        _occupation_contexts("respiratory therapist", "sector.healthcare_support"),
        21,
    ),
    OccupationOption(
        "occupation.radiologic_technologist",
        "radiologic technologist",
        "sector.healthcare_support",
        _education_options("radiologic technologist", "sector.healthcare_support"),
        _occupation_contexts("radiologic technologist", "sector.healthcare_support"),
        21,
    ),
    OccupationOption(
        "occupation.surgical_technician",
        "surgical technician",
        "sector.healthcare_support",
        _education_options("surgical technician", "sector.healthcare_support"),
        _occupation_contexts("surgical technician", "sector.healthcare_support"),
        19,
    ),
    OccupationOption(
        "occupation.emt",
        "EMT",
        "sector.healthcare_support",
        _education_options("EMT", "sector.healthcare_support"),
        _occupation_contexts("EMT", "sector.healthcare_support"),
        18,
    ),
    OccupationOption(
        "occupation.registered_nurse",
        "registered nurse",
        "sector.healthcare_professional",
        _education_options("registered nurse", "sector.healthcare_professional"),
        _occupation_contexts("registered nurse", "sector.healthcare_professional"),
        21,
    ),
    OccupationOption(
        "occupation.dental_hygienist",
        "dental hygienist",
        "sector.healthcare_professional",
        _education_options("dental hygienist", "sector.healthcare_professional"),
        _occupation_contexts("dental hygienist", "sector.healthcare_professional"),
        21,
    ),
    OccupationOption(
        "occupation.physical_therapist",
        "physical therapist",
        "sector.healthcare_professional",
        _education_options("physical therapist", "sector.healthcare_professional"),
        _occupation_contexts("physical therapist", "sector.healthcare_professional"),
        28,
    ),
    OccupationOption(
        "occupation.occupational_therapist",
        "occupational therapist",
        "sector.healthcare_professional",
        _education_options("occupational therapist", "sector.healthcare_professional"),
        _occupation_contexts(
            "occupational therapist", "sector.healthcare_professional"
        ),
        27,
    ),
    OccupationOption(
        "occupation.speech_language_pathologist",
        "speech-language pathologist",
        "sector.healthcare_professional",
        _education_options(
            "speech-language pathologist", "sector.healthcare_professional"
        ),
        _occupation_contexts(
            "speech-language pathologist", "sector.healthcare_professional"
        ),
        26,
    ),
    OccupationOption(
        "occupation.paramedic",
        "paramedic",
        "sector.healthcare_professional",
        _education_options("paramedic", "sector.healthcare_professional"),
        _occupation_contexts("paramedic", "sector.healthcare_professional"),
        20,
    ),
    OccupationOption(
        "occupation.mental_health_counselor",
        "mental-health counselor",
        "sector.healthcare_professional",
        _education_options("mental-health counselor", "sector.healthcare_professional"),
        _occupation_contexts(
            "mental-health counselor", "sector.healthcare_professional"
        ),
        26,
    ),
    OccupationOption(
        "occupation.dietitian",
        "dietitian",
        "sector.healthcare_professional",
        _education_options("dietitian", "sector.healthcare_professional"),
        _occupation_contexts("dietitian", "sector.healthcare_professional"),
        23,
    ),
    OccupationOption(
        "occupation.optician",
        "optician",
        "sector.healthcare_professional",
        _education_options("optician", "sector.healthcare_professional"),
        _occupation_contexts("optician", "sector.healthcare_professional"),
        18,
    ),
    OccupationOption(
        "occupation.veterinarian",
        "veterinarian",
        "sector.healthcare_professional",
        _education_options("veterinarian", "sector.healthcare_professional"),
        _occupation_contexts("veterinarian", "sector.healthcare_professional"),
        26,
    ),
    OccupationOption(
        "occupation.preschool_teacher",
        "preschool teacher",
        "sector.education_and_community",
        _education_options("preschool teacher", "sector.education_and_community"),
        _occupation_contexts("preschool teacher", "sector.education_and_community"),
        19,
    ),
    OccupationOption(
        "occupation.elementary_school_teacher",
        "elementary-school teacher",
        "sector.education_and_community",
        _education_options(
            "elementary-school teacher", "sector.education_and_community"
        ),
        _occupation_contexts(
            "elementary-school teacher", "sector.education_and_community"
        ),
        22,
    ),
    OccupationOption(
        "occupation.high_school_teacher",
        "high-school teacher",
        "sector.education_and_community",
        _education_options("high-school teacher", "sector.education_and_community"),
        _occupation_contexts("high-school teacher", "sector.education_and_community"),
        22,
    ),
    OccupationOption(
        "occupation.special_education_paraprofessional",
        "special-education paraprofessional",
        "sector.education_and_community",
        _education_options(
            "special-education paraprofessional", "sector.education_and_community"
        ),
        _occupation_contexts(
            "special-education paraprofessional", "sector.education_and_community"
        ),
        18,
    ),
    OccupationOption(
        "occupation.school_custodian",
        "school custodian",
        "sector.education_and_community",
        _education_options("school custodian", "sector.education_and_community"),
        _occupation_contexts("school custodian", "sector.education_and_community"),
        18,
    ),
    OccupationOption(
        "occupation.social_worker",
        "social worker",
        "sector.education_and_community",
        _education_options("social worker", "sector.education_and_community"),
        _occupation_contexts("social worker", "sector.education_and_community"),
        23,
    ),
    OccupationOption(
        "occupation.youth_counselor",
        "youth counselor",
        "sector.education_and_community",
        _education_options("youth counselor", "sector.education_and_community"),
        _occupation_contexts("youth counselor", "sector.education_and_community"),
        20,
    ),
    OccupationOption(
        "occupation.academic_adviser",
        "academic adviser",
        "sector.education_and_community",
        _education_options("academic adviser", "sector.education_and_community"),
        _occupation_contexts("academic adviser", "sector.education_and_community"),
        22,
    ),
    OccupationOption(
        "occupation.translator",
        "translator",
        "sector.education_and_community",
        _education_options("translator", "sector.education_and_community"),
        _occupation_contexts("translator", "sector.education_and_community"),
        18,
    ),
    OccupationOption(
        "occupation.community_organizer",
        "community organizer",
        "sector.education_and_community",
        _education_options("community organizer", "sector.education_and_community"),
        _occupation_contexts("community organizer", "sector.education_and_community"),
        18,
    ),
    OccupationOption(
        "occupation.firefighter",
        "firefighter",
        "sector.public_service_and_safety",
        _education_options("firefighter", "sector.public_service_and_safety"),
        _occupation_contexts("firefighter", "sector.public_service_and_safety"),
        19,
        57,
    ),
    OccupationOption(
        "occupation.police_dispatcher",
        "police dispatcher",
        "sector.public_service_and_safety",
        _education_options("police dispatcher", "sector.public_service_and_safety"),
        _occupation_contexts("police dispatcher", "sector.public_service_and_safety"),
        18,
    ),
    OccupationOption(
        "occupation.court_clerk",
        "court clerk",
        "sector.public_service_and_safety",
        _education_options("court clerk", "sector.public_service_and_safety"),
        _occupation_contexts("court clerk", "sector.public_service_and_safety"),
        18,
    ),
    OccupationOption(
        "occupation.correctional_officer",
        "correctional officer",
        "sector.public_service_and_safety",
        _education_options("correctional officer", "sector.public_service_and_safety"),
        _occupation_contexts(
            "correctional officer", "sector.public_service_and_safety"
        ),
        19,
    ),
    OccupationOption(
        "occupation.public_health_inspector",
        "public-health inspector",
        "sector.public_service_and_safety",
        _education_options(
            "public-health inspector", "sector.public_service_and_safety"
        ),
        _occupation_contexts(
            "public-health inspector", "sector.public_service_and_safety"
        ),
        20,
    ),
    OccupationOption(
        "occupation.sanitation_inspector",
        "sanitation inspector",
        "sector.public_service_and_safety",
        _education_options("sanitation inspector", "sector.public_service_and_safety"),
        _occupation_contexts(
            "sanitation inspector", "sector.public_service_and_safety"
        ),
        20,
    ),
    OccupationOption(
        "occupation.emergency_manager",
        "emergency manager",
        "sector.public_service_and_safety",
        _education_options("emergency manager", "sector.public_service_and_safety"),
        _occupation_contexts("emergency manager", "sector.public_service_and_safety"),
        27,
    ),
    OccupationOption(
        "occupation.postal_clerk",
        "postal clerk",
        "sector.public_service_and_safety",
        _education_options("postal clerk", "sector.public_service_and_safety"),
        _occupation_contexts("postal clerk", "sector.public_service_and_safety"),
        18,
    ),
    OccupationOption(
        "occupation.park_ranger",
        "park ranger",
        "sector.public_service_and_safety",
        _education_options("park ranger", "sector.public_service_and_safety"),
        _occupation_contexts("park ranger", "sector.public_service_and_safety"),
        22,
    ),
    OccupationOption(
        "occupation.911_operator",
        "911 operator",
        "sector.public_service_and_safety",
        _education_options("911 operator", "sector.public_service_and_safety"),
        _occupation_contexts("911 operator", "sector.public_service_and_safety"),
        18,
    ),
    OccupationOption(
        "occupation.receptionist",
        "receptionist",
        "sector.office_customer_and_finance",
        _education_options("receptionist", "sector.office_customer_and_finance"),
        _occupation_contexts("receptionist", "sector.office_customer_and_finance"),
        18,
    ),
    OccupationOption(
        "occupation.payroll_clerk",
        "payroll clerk",
        "sector.office_customer_and_finance",
        _education_options("payroll clerk", "sector.office_customer_and_finance"),
        _occupation_contexts("payroll clerk", "sector.office_customer_and_finance"),
        18,
    ),
    OccupationOption(
        "occupation.claims_processor",
        "claims processor",
        "sector.office_customer_and_finance",
        _education_options("claims processor", "sector.office_customer_and_finance"),
        _occupation_contexts("claims processor", "sector.office_customer_and_finance"),
        18,
    ),
    OccupationOption(
        "occupation.customer_support_representative",
        "customer-support representative",
        "sector.office_customer_and_finance",
        _education_options(
            "customer-support representative", "sector.office_customer_and_finance"
        ),
        _occupation_contexts(
            "customer-support representative", "sector.office_customer_and_finance"
        ),
        18,
    ),
    OccupationOption(
        "occupation.legal_assistant",
        "legal assistant",
        "sector.office_customer_and_finance",
        _education_options("legal assistant", "sector.office_customer_and_finance"),
        _occupation_contexts("legal assistant", "sector.office_customer_and_finance"),
        19,
    ),
    OccupationOption(
        "occupation.bookkeeper",
        "bookkeeper",
        "sector.office_customer_and_finance",
        _education_options("bookkeeper", "sector.office_customer_and_finance"),
        _occupation_contexts("bookkeeper", "sector.office_customer_and_finance"),
        18,
    ),
    OccupationOption(
        "occupation.medical_scheduler",
        "medical scheduler",
        "sector.office_customer_and_finance",
        _education_options("medical scheduler", "sector.office_customer_and_finance"),
        _occupation_contexts("medical scheduler", "sector.office_customer_and_finance"),
        18,
    ),
    OccupationOption(
        "occupation.records_clerk",
        "records clerk",
        "sector.office_customer_and_finance",
        _education_options("records clerk", "sector.office_customer_and_finance"),
        _occupation_contexts("records clerk", "sector.office_customer_and_finance"),
        18,
    ),
    OccupationOption(
        "occupation.hr_coordinator",
        "HR coordinator",
        "sector.office_customer_and_finance",
        _education_options("HR coordinator", "sector.office_customer_and_finance"),
        _occupation_contexts("HR coordinator", "sector.office_customer_and_finance"),
        20,
    ),
    OccupationOption(
        "occupation.loan_processor",
        "loan processor",
        "sector.office_customer_and_finance",
        _education_options("loan processor", "sector.office_customer_and_finance"),
        _occupation_contexts("loan processor", "sector.office_customer_and_finance"),
        18,
    ),
    OccupationOption(
        "occupation.farmhand",
        "farmhand",
        "sector.agriculture_and_environment",
        _education_options("farmhand", "sector.agriculture_and_environment"),
        _occupation_contexts("farmhand", "sector.agriculture_and_environment"),
        18,
    ),
    OccupationOption(
        "occupation.dairy_worker",
        "dairy worker",
        "sector.agriculture_and_environment",
        _education_options("dairy worker", "sector.agriculture_and_environment"),
        _occupation_contexts("dairy worker", "sector.agriculture_and_environment"),
        18,
    ),
    OccupationOption(
        "occupation.greenhouse_grower",
        "greenhouse grower",
        "sector.agriculture_and_environment",
        _education_options("greenhouse grower", "sector.agriculture_and_environment"),
        _occupation_contexts("greenhouse grower", "sector.agriculture_and_environment"),
        18,
    ),
    OccupationOption(
        "occupation.landscaper",
        "landscaper",
        "sector.agriculture_and_environment",
        _education_options("landscaper", "sector.agriculture_and_environment"),
        _occupation_contexts("landscaper", "sector.agriculture_and_environment"),
        18,
    ),
    OccupationOption(
        "occupation.groundskeeper",
        "groundskeeper",
        "sector.agriculture_and_environment",
        _education_options("groundskeeper", "sector.agriculture_and_environment"),
        _occupation_contexts("groundskeeper", "sector.agriculture_and_environment"),
        18,
    ),
    OccupationOption(
        "occupation.forestry_technician",
        "forestry technician",
        "sector.agriculture_and_environment",
        _education_options("forestry technician", "sector.agriculture_and_environment"),
        _occupation_contexts(
            "forestry technician", "sector.agriculture_and_environment"
        ),
        20,
    ),
    OccupationOption(
        "occupation.fisheries_technician",
        "fisheries technician",
        "sector.agriculture_and_environment",
        _education_options(
            "fisheries technician", "sector.agriculture_and_environment"
        ),
        _occupation_contexts(
            "fisheries technician", "sector.agriculture_and_environment"
        ),
        18,
    ),
    OccupationOption(
        "occupation.recycling_sorter",
        "recycling sorter",
        "sector.agriculture_and_environment",
        _education_options("recycling sorter", "sector.agriculture_and_environment"),
        _occupation_contexts("recycling sorter", "sector.agriculture_and_environment"),
        18,
    ),
    OccupationOption(
        "occupation.waste_collector",
        "waste collector",
        "sector.agriculture_and_environment",
        _education_options("waste collector", "sector.agriculture_and_environment"),
        _occupation_contexts("waste collector", "sector.agriculture_and_environment"),
        18,
    ),
    OccupationOption(
        "occupation.pest_control_technician",
        "pest-control technician",
        "sector.agriculture_and_environment",
        _education_options(
            "pest-control technician", "sector.agriculture_and_environment"
        ),
        _occupation_contexts(
            "pest-control technician", "sector.agriculture_and_environment"
        ),
        18,
    ),
    OccupationOption(
        "occupation.laboratory_technician",
        "laboratory technician",
        "sector.science_technical_and_professional",
        _education_options(
            "laboratory technician", "sector.science_technical_and_professional"
        ),
        _occupation_contexts(
            "laboratory technician", "sector.science_technical_and_professional"
        ),
        19,
    ),
    OccupationOption(
        "occupation.gis_technician",
        "GIS technician",
        "sector.science_technical_and_professional",
        _education_options(
            "GIS technician", "sector.science_technical_and_professional"
        ),
        _occupation_contexts(
            "GIS technician", "sector.science_technical_and_professional"
        ),
        19,
    ),
    OccupationOption(
        "occupation.civil_engineering_technician",
        "civil-engineering technician",
        "sector.science_technical_and_professional",
        _education_options(
            "civil-engineering technician", "sector.science_technical_and_professional"
        ),
        _occupation_contexts(
            "civil-engineering technician", "sector.science_technical_and_professional"
        ),
        19,
    ),
    OccupationOption(
        "occupation.accountant",
        "accountant",
        "sector.science_technical_and_professional",
        _education_options("accountant", "sector.science_technical_and_professional"),
        _occupation_contexts("accountant", "sector.science_technical_and_professional"),
        21,
    ),
    OccupationOption(
        "occupation.paralegal",
        "paralegal",
        "sector.science_technical_and_professional",
        _education_options("paralegal", "sector.science_technical_and_professional"),
        _occupation_contexts("paralegal", "sector.science_technical_and_professional"),
        19,
    ),
    OccupationOption(
        "occupation.insurance_underwriter",
        "insurance underwriter",
        "sector.science_technical_and_professional",
        _education_options(
            "insurance underwriter", "sector.science_technical_and_professional"
        ),
        _occupation_contexts(
            "insurance underwriter", "sector.science_technical_and_professional"
        ),
        20,
    ),
    OccupationOption(
        "occupation.urban_planner",
        "urban planner",
        "sector.science_technical_and_professional",
        _education_options(
            "urban planner", "sector.science_technical_and_professional"
        ),
        _occupation_contexts(
            "urban planner", "sector.science_technical_and_professional"
        ),
        24,
    ),
    OccupationOption(
        "occupation.chemist",
        "chemist",
        "sector.science_technical_and_professional",
        _education_options("chemist", "sector.science_technical_and_professional"),
        _occupation_contexts("chemist", "sector.science_technical_and_professional"),
        22,
    ),
    OccupationOption(
        "occupation.statistician",
        "statistician",
        "sector.science_technical_and_professional",
        _education_options("statistician", "sector.science_technical_and_professional"),
        _occupation_contexts(
            "statistician", "sector.science_technical_and_professional"
        ),
        22,
    ),
    OccupationOption(
        "occupation.land_surveyor",
        "land surveyor",
        "sector.science_technical_and_professional",
        _education_options(
            "land surveyor", "sector.science_technical_and_professional"
        ),
        _occupation_contexts(
            "land surveyor", "sector.science_technical_and_professional"
        ),
        24,
    ),
    OccupationOption(
        "occupation.graphic_designer",
        "graphic designer",
        "sector.creative_media_and_culture",
        _education_options("graphic designer", "sector.creative_media_and_culture"),
        _occupation_contexts("graphic designer", "sector.creative_media_and_culture"),
        20,
    ),
    OccupationOption(
        "occupation.photographer",
        "photographer",
        "sector.creative_media_and_culture",
        _education_options("photographer", "sector.creative_media_and_culture"),
        _occupation_contexts("photographer", "sector.creative_media_and_culture"),
        18,
    ),
    OccupationOption(
        "occupation.audio_technician",
        "audio technician",
        "sector.creative_media_and_culture",
        _education_options("audio technician", "sector.creative_media_and_culture"),
        _occupation_contexts("audio technician", "sector.creative_media_and_culture"),
        18,
    ),
    OccupationOption(
        "occupation.stagehand",
        "stagehand",
        "sector.creative_media_and_culture",
        _education_options("stagehand", "sector.creative_media_and_culture"),
        _occupation_contexts("stagehand", "sector.creative_media_and_culture"),
        18,
    ),
    OccupationOption(
        "occupation.copy_editor",
        "copy editor",
        "sector.creative_media_and_culture",
        _education_options("copy editor", "sector.creative_media_and_culture"),
        _occupation_contexts("copy editor", "sector.creative_media_and_culture"),
        21,
    ),
    OccupationOption(
        "occupation.sign_painter",
        "sign painter",
        "sector.creative_media_and_culture",
        _education_options("sign painter", "sector.creative_media_and_culture"),
        _occupation_contexts("sign painter", "sector.creative_media_and_culture"),
        18,
    ),
    OccupationOption(
        "occupation.florist",
        "florist",
        "sector.creative_media_and_culture",
        _education_options("florist", "sector.creative_media_and_culture"),
        _occupation_contexts("florist", "sector.creative_media_and_culture"),
        18,
    ),
    OccupationOption(
        "occupation.tailor",
        "tailor",
        "sector.creative_media_and_culture",
        _education_options("tailor", "sector.creative_media_and_culture"),
        _occupation_contexts("tailor", "sector.creative_media_and_culture"),
        18,
    ),
    OccupationOption(
        "occupation.community_radio_producer",
        "community-radio producer",
        "sector.creative_media_and_culture",
        _education_options(
            "community-radio producer", "sector.creative_media_and_culture"
        ),
        _occupation_contexts(
            "community-radio producer", "sector.creative_media_and_culture"
        ),
        18,
    ),
    OccupationOption(
        "occupation.wedding_dj",
        "wedding DJ",
        "sector.creative_media_and_culture",
        _education_options("wedding DJ", "sector.creative_media_and_culture"),
        _occupation_contexts("wedding DJ", "sector.creative_media_and_culture"),
        18,
    ),
    OccupationOption(
        "occupation.help_desk_technician",
        "help-desk technician",
        "sector.technology_and_digital",
        _education_options("help-desk technician", "sector.technology_and_digital"),
        _occupation_contexts("help-desk technician", "sector.technology_and_digital"),
        18,
    ),
    OccupationOption(
        "occupation.network_technician",
        "network technician",
        "sector.technology_and_digital",
        _education_options("network technician", "sector.technology_and_digital"),
        _occupation_contexts("network technician", "sector.technology_and_digital"),
        18,
    ),
    OccupationOption(
        "occupation.systems_administrator",
        "systems administrator",
        "sector.technology_and_digital",
        _education_options("systems administrator", "sector.technology_and_digital"),
        _occupation_contexts("systems administrator", "sector.technology_and_digital"),
        22,
    ),
    OccupationOption(
        "occupation.web_developer",
        "web developer",
        "sector.technology_and_digital",
        _education_options("web developer", "sector.technology_and_digital"),
        _occupation_contexts("web developer", "sector.technology_and_digital"),
        20,
    ),
    OccupationOption(
        "occupation.software_engineer",
        "software engineer",
        "sector.technology_and_digital",
        _education_options("software engineer", "sector.technology_and_digital"),
        _occupation_contexts("software engineer", "sector.technology_and_digital"),
        21,
    ),
    OccupationOption(
        "occupation.qa_analyst",
        "QA analyst",
        "sector.technology_and_digital",
        _education_options("QA analyst", "sector.technology_and_digital"),
        _occupation_contexts("QA analyst", "sector.technology_and_digital"),
        20,
    ),
    OccupationOption(
        "occupation.data_analyst",
        "data analyst",
        "sector.technology_and_digital",
        _education_options("data analyst", "sector.technology_and_digital"),
        _occupation_contexts("data analyst", "sector.technology_and_digital"),
        21,
    ),
    OccupationOption(
        "occupation.cybersecurity_analyst",
        "cybersecurity analyst",
        "sector.technology_and_digital",
        _education_options("cybersecurity analyst", "sector.technology_and_digital"),
        _occupation_contexts("cybersecurity analyst", "sector.technology_and_digital"),
        20,
    ),
    OccupationOption(
        "occupation.ux_researcher",
        "UX researcher",
        "sector.technology_and_digital",
        _education_options("UX researcher", "sector.technology_and_digital"),
        _occupation_contexts("UX researcher", "sector.technology_and_digital"),
        21,
    ),
    OccupationOption(
        "occupation.it_trainer",
        "IT trainer",
        "sector.technology_and_digital",
        _education_options("IT trainer", "sector.technology_and_digital"),
        _occupation_contexts("IT trainer", "sector.technology_and_digital"),
        19,
    ),
    OccupationOption(
        "occupation.rideshare_driver",
        "rideshare driver",
        "sector.independent_and_irregular_work",
        _education_options("rideshare driver", "sector.independent_and_irregular_work"),
        _occupation_contexts(
            "rideshare driver", "sector.independent_and_irregular_work"
        ),
        21,
    ),
    OccupationOption(
        "occupation.market_vendor",
        "market vendor",
        "sector.independent_and_irregular_work",
        _education_options("market vendor", "sector.independent_and_irregular_work"),
        _occupation_contexts("market vendor", "sector.independent_and_irregular_work"),
        18,
    ),
    OccupationOption(
        "occupation.house_cleaner",
        "house cleaner",
        "sector.independent_and_irregular_work",
        _education_options("house cleaner", "sector.independent_and_irregular_work"),
        _occupation_contexts("house cleaner", "sector.independent_and_irregular_work"),
        18,
    ),
    OccupationOption(
        "occupation.handyman",
        "handyman",
        "sector.independent_and_irregular_work",
        _education_options("handyman", "sector.independent_and_irregular_work"),
        _occupation_contexts("handyman", "sector.independent_and_irregular_work"),
        19,
    ),
    OccupationOption(
        "occupation.pet_sitter",
        "pet sitter",
        "sector.independent_and_irregular_work",
        _education_options("pet sitter", "sector.independent_and_irregular_work"),
        _occupation_contexts("pet sitter", "sector.independent_and_irregular_work"),
        18,
    ),
    OccupationOption(
        "occupation.seasonal_resort_worker",
        "seasonal resort worker",
        "sector.independent_and_irregular_work",
        _education_options(
            "seasonal resort worker", "sector.independent_and_irregular_work"
        ),
        _occupation_contexts(
            "seasonal resort worker", "sector.independent_and_irregular_work"
        ),
        18,
    ),
    OccupationOption(
        "occupation.childcare_provider",
        "childcare provider",
        "sector.independent_and_irregular_work",
        _education_options(
            "childcare provider", "sector.independent_and_irregular_work"
        ),
        _occupation_contexts(
            "childcare provider", "sector.independent_and_irregular_work"
        ),
        19,
    ),
    OccupationOption(
        "occupation.online_reseller",
        "online reseller",
        "sector.independent_and_irregular_work",
        _education_options("online reseller", "sector.independent_and_irregular_work"),
        _occupation_contexts(
            "online reseller", "sector.independent_and_irregular_work"
        ),
        18,
    ),
    OccupationOption(
        "occupation.mobile_notary",
        "mobile notary",
        "sector.independent_and_irregular_work",
        _education_options("mobile notary", "sector.independent_and_irregular_work"),
        _occupation_contexts("mobile notary", "sector.independent_and_irregular_work"),
        21,
    ),
    OccupationOption(
        "occupation.food_delivery_courier",
        "food-delivery courier",
        "sector.independent_and_irregular_work",
        _education_options(
            "food-delivery courier", "sector.independent_and_irregular_work"
        ),
        _occupation_contexts(
            "food-delivery courier", "sector.independent_and_irregular_work"
        ),
        21,
    ),
)
TRAIT_AXES = (
    "axis.social_energy",
    "axis.warmth_and_conflict",
    "axis.organization_and_reliability",
    "axis.affect_and_outlook",
    "axis.openness_and_decisions",
    "axis.humor",
    "axis.interpersonal_quirks",
    "axis.motivation_and_values",
)
TRAITS = (
    TraitOption("trait.reserved", "reserved", "axis.social_energy", False),
    TraitOption("trait.outgoing", "outgoing", "axis.social_energy", False),
    TraitOption("trait.chatty", "chatty", "axis.social_energy", False),
    TraitOption("trait.private", "private", "axis.social_energy", False),
    TraitOption(
        "trait.sociable_in_small_groups",
        "sociable in small groups",
        "axis.social_energy",
        False,
    ),
    TraitOption("trait.solitary", "solitary", "axis.social_energy", False),
    TraitOption(
        "trait.shy_with_strangers", "shy with strangers", "axis.social_energy", False
    ),
    TraitOption(
        "trait.comfortable_with_crowds",
        "comfortable with crowds",
        "axis.social_energy",
        False,
    ),
    TraitOption(
        "trait.prefers_listening", "prefers listening", "axis.social_energy", False
    ),
    TraitOption(
        "trait.attention_seeking", "attention-seeking", "axis.social_energy", True
    ),
    TraitOption(
        "trait.slow_to_warm_up", "slow to warm up", "axis.social_energy", False
    ),
    TraitOption(
        "trait.energized_by_company",
        "energized by company",
        "axis.social_energy",
        False,
    ),
    TraitOption("trait.warm", "warm", "axis.warmth_and_conflict", False),
    TraitOption("trait.tactful", "tactful", "axis.warmth_and_conflict", False),
    TraitOption("trait.blunt", "blunt", "axis.warmth_and_conflict", True),
    TraitOption(
        "trait.accommodating", "accommodating", "axis.warmth_and_conflict", False
    ),
    TraitOption("trait.skeptical", "skeptical", "axis.warmth_and_conflict", False),
    TraitOption("trait.cooperative", "cooperative", "axis.warmth_and_conflict", False),
    TraitOption("trait.stubborn", "stubborn", "axis.warmth_and_conflict", True),
    TraitOption(
        "trait.conciliatory", "conciliatory", "axis.warmth_and_conflict", False
    ),
    TraitOption(
        "trait.argumentative", "argumentative", "axis.warmth_and_conflict", True
    ),
    TraitOption(
        "trait.conflict_avoidant",
        "conflict-avoidant",
        "axis.warmth_and_conflict",
        False,
    ),
    TraitOption(
        "trait.quick_to_apologize",
        "quick to apologize",
        "axis.warmth_and_conflict",
        False,
    ),
    TraitOption(
        "trait.holds_grudges", "holds grudges", "axis.warmth_and_conflict", True
    ),
    TraitOption(
        "trait.methodical", "methodical", "axis.organization_and_reliability", False
    ),
    TraitOption(
        "trait.spontaneous", "spontaneous", "axis.organization_and_reliability", False
    ),
    TraitOption(
        "trait.punctual", "punctual", "axis.organization_and_reliability", False
    ),
    TraitOption(
        "trait.chronic_procrastinator",
        "chronic procrastinator",
        "axis.organization_and_reliability",
        True,
    ),
    TraitOption(
        "trait.meticulous", "meticulous", "axis.organization_and_reliability", False
    ),
    TraitOption("trait.messy", "messy", "axis.organization_and_reliability", True),
    TraitOption(
        "trait.dependable", "dependable", "axis.organization_and_reliability", False
    ),
    TraitOption(
        "trait.easily_distracted",
        "easily distracted",
        "axis.organization_and_reliability",
        True,
    ),
    TraitOption(
        "trait.routine_driven",
        "routine-driven",
        "axis.organization_and_reliability",
        False,
    ),
    TraitOption(
        "trait.improvisational",
        "improvisational",
        "axis.organization_and_reliability",
        False,
    ),
    TraitOption(
        "trait.overcommitted",
        "overcommitted",
        "axis.organization_and_reliability",
        True,
    ),
    TraitOption(
        "trait.forgetful", "forgetful", "axis.organization_and_reliability", True
    ),
    TraitOption("trait.optimistic", "optimistic", "axis.affect_and_outlook", False),
    TraitOption("trait.cynical", "cynical", "axis.affect_and_outlook", True),
    TraitOption("trait.anxious", "anxious", "axis.affect_and_outlook", True),
    TraitOption(
        "trait.even_tempered", "even-tempered", "axis.affect_and_outlook", False
    ),
    TraitOption("trait.excitable", "excitable", "axis.affect_and_outlook", False),
    TraitOption("trait.stoic", "stoic", "axis.affect_and_outlook", False),
    TraitOption("trait.sentimental", "sentimental", "axis.affect_and_outlook", False),
    TraitOption("trait.irritable", "irritable", "axis.affect_and_outlook", True),
    TraitOption("trait.patient", "patient", "axis.affect_and_outlook", False),
    TraitOption(
        "trait.easily_discouraged",
        "easily discouraged",
        "axis.affect_and_outlook",
        True,
    ),
    TraitOption("trait.resilient", "resilient", "axis.affect_and_outlook", False),
    TraitOption("trait.suspicious", "suspicious", "axis.affect_and_outlook", True),
    TraitOption("trait.practical", "practical", "axis.openness_and_decisions", False),
    TraitOption(
        "trait.imaginative", "imaginative", "axis.openness_and_decisions", False
    ),
    TraitOption(
        "trait.conventional", "conventional", "axis.openness_and_decisions", False
    ),
    TraitOption(
        "trait.experimental", "experimental", "axis.openness_and_decisions", False
    ),
    TraitOption("trait.cautious", "cautious", "axis.openness_and_decisions", False),
    TraitOption(
        "trait.novelty_seeking", "novelty-seeking", "axis.openness_and_decisions", False
    ),
    TraitOption("trait.nostalgic", "nostalgic", "axis.openness_and_decisions", False),
    TraitOption(
        "trait.detail_focused", "detail-focused", "axis.openness_and_decisions", False
    ),
    TraitOption(
        "trait.big_picture", "big-picture", "axis.openness_and_decisions", False
    ),
    TraitOption("trait.indecisive", "indecisive", "axis.openness_and_decisions", True),
    TraitOption("trait.decisive", "decisive", "axis.openness_and_decisions", False),
    TraitOption(
        "trait.niche_obsessed", "niche-obsessed", "axis.openness_and_decisions", False
    ),
    TraitOption("trait.dry", "dry", "axis.humor", False),
    TraitOption("trait.silly", "silly", "axis.humor", False),
    TraitOption("trait.sarcastic", "sarcastic", "axis.humor", False),
    TraitOption("trait.earnest", "earnest", "axis.humor", False),
    TraitOption("trait.deadpan", "deadpan", "axis.humor", False),
    TraitOption("trait.pun_heavy", "pun-heavy", "axis.humor", True),
    TraitOption("trait.self_deprecating", "self-deprecating", "axis.humor", False),
    TraitOption("trait.teasing", "teasing", "axis.humor", True),
    TraitOption("trait.absurdist", "absurdist", "axis.humor", False),
    TraitOption("trait.rarely_jokes", "rarely jokes", "axis.humor", False),
    TraitOption("trait.gallows_humor", "gallows humor", "axis.humor", True),
    TraitOption("trait.wholesome", "wholesome", "axis.humor", False),
    TraitOption("trait.interrupts", "interrupts", "axis.interpersonal_quirks", True),
    TraitOption(
        "trait.overexplains", "overexplains", "axis.interpersonal_quirks", True
    ),
    TraitOption(
        "trait.people_pleases", "people-pleases", "axis.interpersonal_quirks", True
    ),
    TraitOption(
        "trait.corrects_minor_details",
        "corrects minor details",
        "axis.interpersonal_quirks",
        True,
    ),
    TraitOption(
        "trait.changes_their_mind_readily",
        "changes their mind readily",
        "axis.interpersonal_quirks",
        False,
    ),
    TraitOption("trait.competitive", "competitive", "axis.interpersonal_quirks", False),
    TraitOption("trait.overshares", "overshares", "axis.interpersonal_quirks", True),
    TraitOption(
        "trait.under_communicates",
        "under-communicates",
        "axis.interpersonal_quirks",
        True,
    ),
    TraitOption(
        "trait.gives_unsolicited_advice",
        "gives unsolicited advice",
        "axis.interpersonal_quirks",
        True,
    ),
    TraitOption(
        "trait.assumes_good_faith",
        "assumes good faith",
        "axis.interpersonal_quirks",
        False,
    ),
    TraitOption(
        "trait.expects_the_worst",
        "expects the worst",
        "axis.interpersonal_quirks",
        True,
    ),
    TraitOption(
        "trait.avoids_asking_for_help",
        "avoids asking for help",
        "axis.interpersonal_quirks",
        True,
    ),
    TraitOption(
        "trait.community_minded",
        "community-minded",
        "axis.motivation_and_values",
        False,
    ),
    TraitOption("trait.ambitious", "ambitious", "axis.motivation_and_values", False),
    TraitOption(
        "trait.security_oriented",
        "security-oriented",
        "axis.motivation_and_values",
        False,
    ),
    TraitOption(
        "trait.status_conscious", "status-conscious", "axis.motivation_and_values", True
    ),
    TraitOption("trait.principled", "principled", "axis.motivation_and_values", False),
    TraitOption("trait.pragmatic", "pragmatic", "axis.motivation_and_values", False),
    TraitOption("trait.frugal", "frugal", "axis.motivation_and_values", False),
    TraitOption("trait.indulgent", "indulgent", "axis.motivation_and_values", True),
    TraitOption(
        "trait.approval_seeking", "approval-seeking", "axis.motivation_and_values", True
    ),
    TraitOption(
        "trait.independent", "independent", "axis.motivation_and_values", False
    ),
    TraitOption(
        "trait.duty_driven", "duty-driven", "axis.motivation_and_values", False
    ),
    TraitOption(
        "trait.easily_bored", "easily bored", "axis.motivation_and_values", True
    ),
)


_CONTRADICTIONS = (
    frozenset(("trait.reserved", "trait.outgoing")),
    frozenset(("trait.reserved", "trait.chatty")),
    frozenset(("trait.solitary", "trait.energized_by_company")),
    frozenset(("trait.shy_with_strangers", "trait.comfortable_with_crowds")),
    frozenset(("trait.conflict_avoidant", "trait.argumentative")),
    frozenset(("trait.tactful", "trait.blunt")),
    frozenset(("trait.quick_to_apologize", "trait.holds_grudges")),
    frozenset(("trait.stubborn", "trait.cooperative")),
    frozenset(("trait.conciliatory", "trait.argumentative")),
    frozenset(("trait.methodical", "trait.spontaneous")),
    frozenset(("trait.punctual", "trait.chronic_procrastinator")),
    frozenset(("trait.meticulous", "trait.messy")),
    frozenset(("trait.routine_driven", "trait.improvisational")),
    frozenset(("trait.optimistic", "trait.cynical")),
    frozenset(("trait.even_tempered", "trait.irritable")),
    frozenset(("trait.patient", "trait.irritable")),
    frozenset(("trait.stoic", "trait.excitable")),
    frozenset(("trait.resilient", "trait.easily_discouraged")),
    frozenset(("trait.cautious", "trait.novelty_seeking")),
    frozenset(("trait.decisive", "trait.indecisive")),
    frozenset(("trait.conventional", "trait.experimental")),
    frozenset(("trait.detail_focused", "trait.big_picture")),
    frozenset(("trait.deadpan", "trait.silly")),
    frozenset(("trait.rarely_jokes", "trait.pun_heavy")),
    frozenset(("trait.earnest", "trait.sarcastic")),
    frozenset(("trait.overshares", "trait.private")),
    frozenset(("trait.under_communicates", "trait.overexplains")),
    frozenset(("trait.assumes_good_faith", "trait.expects_the_worst")),
    frozenset(("trait.frugal", "trait.indulgent")),
    frozenset(("trait.independent", "trait.approval_seeking")),
)
CONTRADICTING_TRAIT_PAIRS = frozenset(_CONTRADICTIONS)

WRITING_STYLES = (
    WritingStyleOption(
        "style.terse_lowercase_a",
        "terse lowercase fragments, rarely more than a few words, minimal punctuation",
        "family.terse_lowercase",
    ),
    WritingStyleOption(
        "style.terse_lowercase_b",
        "terse lowercase observations with clipped wording and no ceremony",
        "family.terse_lowercase",
    ),
    WritingStyleOption(
        "style.short_standard_caps_a",
        "short standard-capitalization replies with one clear point",
        "family.short_standard_caps",
    ),
    WritingStyleOption(
        "style.short_standard_caps_b",
        "brief polished sentences with normal capitalization",
        "family.short_standard_caps",
    ),
    WritingStyleOption(
        "style.typo_prone_phone_a",
        "casual phone typing with occasional harmless typos and dropped punctuation",
        "family.typo_prone_phone",
    ),
    WritingStyleOption(
        "style.typo_prone_phone_b",
        "fast mobile messages, abbreviations, and the occasional misspelled word",
        "family.typo_prone_phone",
    ),
    WritingStyleOption(
        "style.jokey_slang_a",
        "jokey slang, playful exaggeration, and informal phrasing",
        "family.jokey_slang",
    ),
    WritingStyleOption(
        "style.jokey_slang_b",
        "internet-casual banter with slang and quick jokes",
        "family.jokey_slang",
    ),
    WritingStyleOption(
        "style.dry_concise_a",
        "dry concise prose that states facts without fuss",
        "family.dry_concise",
    ),
    WritingStyleOption(
        "style.dry_concise_b",
        "understated, economical replies with a deadpan edge",
        "family.dry_concise",
    ),
    WritingStyleOption(
        "style.chatty_run_on_a",
        "chatty run-on sentences that keep adding side thoughts",
        "family.chatty_run_on",
    ),
    WritingStyleOption(
        "style.chatty_run_on_b",
        "long conversational sentences with frequent asides",
        "family.chatty_run_on",
    ),
    WritingStyleOption(
        "style.earnest_conversational_a",
        "earnest conversational replies that acknowledge other viewpoints",
        "family.earnest_conversational",
    ),
    WritingStyleOption(
        "style.earnest_conversational_b",
        "sincere, friendly explanations in natural spoken language",
        "family.earnest_conversational",
    ),
    WritingStyleOption(
        "style.precise_technical_a",
        "precise technical explanations with careful terminology",
        "family.precise_technical",
    ),
    WritingStyleOption(
        "style.precise_technical_b",
        "structured reasoning that defines terms before using them",
        "family.precise_technical",
    ),
    WritingStyleOption(
        "style.source_caveats_a",
        "source-linking caveats and careful distinctions between evidence and opinion",
        "family.source_caveats",
    ),
    WritingStyleOption(
        "style.source_caveats_b",
        "cautious claims that mention sources, uncertainty, and limitations",
        "family.source_caveats",
    ),
    WritingStyleOption(
        "style.structured_bullets_a",
        "structured bullet points with compact labels",
        "family.structured_bullets",
    ),
    WritingStyleOption(
        "style.structured_bullets_b",
        "organized numbered points and clear headings",
        "family.structured_bullets",
    ),
    WritingStyleOption(
        "style.reflective_storytelling_a",
        "reflective storytelling with sensory details and personal context",
        "family.reflective_storytelling",
    ),
    WritingStyleOption(
        "style.reflective_storytelling_b",
        "thoughtful first-person anecdotes that connect events to lessons",
        "family.reflective_storytelling",
    ),
    WritingStyleOption(
        "style.verbose_digression_a",
        "verbose digressions that wander through relevant background",
        "family.verbose_digression",
    ),
    WritingStyleOption(
        "style.emphatic_punctuation_a",
        "emphatic punctuation, dramatic pauses, and occasional all-caps emphasis",
        "family.emphatic_punctuation",
    ),
    WritingStyleOption(
        "style.understated_plain_a",
        "understated plain prose with simple literal wording",
        "family.understated_plain",
    ),
    WritingStyleOption(
        "style.occasional_emoji_a",
        "conversational prose with an occasional fitting emoji",
        "family.occasional_emoji",
    ),
    WritingStyleOption(
        "style.question_led_a",
        "question-led conversation that explores an issue by asking focused questions",
        "family.question_led",
    ),
)
INTEREST_DOMAINS = (
    "domain.home",
    "domain.outdoors",
    "domain.crafts",
    "domain.sports",
    "domain.games",
    "domain.music",
    "domain.reading",
    "domain.food",
    "domain.transport",
    "domain.collecting",
    "domain.volunteering",
    "domain.local_life",
    "domain.science",
    "domain.everyday_low_cost",
)
INTERESTS = (
    InterestOption(
        "interest.container_gardening", "container gardening", "domain.home"
    ),
    InterestOption(
        "interest.houseplant_propagation", "houseplant propagation", "domain.home"
    ),
    InterestOption(
        "interest.decluttering_one_drawer", "decluttering one drawer", "domain.home"
    ),
    InterestOption("interest.diy_shelf_repair", "DIY shelf repair", "domain.home"),
    InterestOption("interest.home_organization", "home organization", "domain.home"),
    InterestOption(
        "interest.watching_home_tours", "watching home tours", "domain.home"
    ),
    InterestOption(
        "interest.indoor_herb_growing", "indoor herb growing", "domain.home"
    ),
    InterestOption("interest.budget_decorating", "budget decorating", "domain.home"),
    InterestOption(
        "interest.fixing_leaky_faucets", "fixing leaky faucets", "domain.home"
    ),
    InterestOption("interest.trail_running", "trail running", "domain.outdoors"),
    InterestOption("interest.birdwatching", "birdwatching", "domain.outdoors"),
    InterestOption("interest.day_hikes", "day hikes", "domain.outdoors"),
    InterestOption("interest.stargazing", "stargazing", "domain.outdoors"),
    InterestOption(
        "interest.camping_at_state_parks", "camping at state parks", "domain.outdoors"
    ),
    InterestOption(
        "interest.urban_nature_walks", "urban nature walks", "domain.outdoors"
    ),
    InterestOption(
        "interest.fishing_at_local_lakes", "fishing at local lakes", "domain.outdoors"
    ),
    InterestOption("interest.geocaching", "geocaching", "domain.outdoors"),
    InterestOption("interest.wildflower_walks", "wildflower walks", "domain.outdoors"),
    InterestOption("interest.knitting", "knitting", "domain.crafts"),
    InterestOption("interest.wood_carving", "wood carving", "domain.crafts"),
    InterestOption("interest.quilting", "quilting", "domain.crafts"),
    InterestOption(
        "interest.watercolor_painting", "watercolor painting", "domain.crafts"
    ),
    InterestOption("interest.leatherworking", "leatherworking", "domain.crafts"),
    InterestOption("interest.model_building", "model building", "domain.crafts"),
    InterestOption("interest.crochet", "crochet", "domain.crafts"),
    InterestOption("interest.ceramics", "ceramics", "domain.crafts"),
    InterestOption("interest.paper_crafts", "paper crafts", "domain.crafts"),
    InterestOption("interest.pickup_basketball", "pickup basketball", "domain.sports"),
    InterestOption(
        "interest.recreational_soccer", "recreational soccer", "domain.sports"
    ),
    InterestOption("interest.swimming_laps", "swimming laps", "domain.sports"),
    InterestOption(
        "interest.community_softball", "community softball", "domain.sports"
    ),
    InterestOption("interest.table_tennis", "table tennis", "domain.sports"),
    InterestOption("interest.yoga_classes", "yoga classes", "domain.sports"),
    InterestOption("interest.cycling", "cycling", "domain.sports"),
    InterestOption("interest.bowling", "bowling", "domain.sports"),
    InterestOption(
        "interest.watching_local_sports", "watching local sports", "domain.sports"
    ),
    InterestOption("interest.chess", "chess", "domain.games"),
    InterestOption("interest.board_game_nights", "board-game nights", "domain.games"),
    InterestOption(
        "interest.cooperative_card_games", "cooperative card games", "domain.games"
    ),
    InterestOption("interest.crossword_puzzles", "crossword puzzles", "domain.games"),
    InterestOption("interest.video_games", "video games", "domain.games"),
    InterestOption(
        "interest.tabletop_roleplaying", "tabletop roleplaying", "domain.games"
    ),
    InterestOption("interest.jigsaw_puzzles", "jigsaw puzzles", "domain.games"),
    InterestOption("interest.arcade_games", "arcade games", "domain.games"),
    InterestOption("interest.daily_word_puzzles", "daily word puzzles", "domain.games"),
    InterestOption("interest.vinyl_records", "vinyl records", "domain.music"),
    InterestOption("interest.guitar_practice", "guitar practice", "domain.music"),
    InterestOption("interest.choir_singing", "choir singing", "domain.music"),
    InterestOption("interest.local_concerts", "local concerts", "domain.music"),
    InterestOption("interest.making_playlists", "making playlists", "domain.music"),
    InterestOption("interest.drumming", "drumming", "domain.music"),
    InterestOption("interest.jazz_radio", "jazz radio", "domain.music"),
    InterestOption("interest.karaoke", "karaoke", "domain.music"),
    InterestOption("interest.learning_piano", "learning piano", "domain.music"),
    InterestOption("interest.mystery_novels", "mystery novels", "domain.reading"),
    InterestOption("interest.science_fiction", "science fiction", "domain.reading"),
    InterestOption("interest.used_bookstores", "used bookstores", "domain.reading"),
    InterestOption("interest.audiobooks", "audiobooks", "domain.reading"),
    InterestOption("interest.history_podcasts", "history podcasts", "domain.reading"),
    InterestOption("interest.poetry", "poetry", "domain.reading"),
    InterestOption("interest.graphic_novels", "graphic novels", "domain.reading"),
    InterestOption(
        "interest.library_book_clubs", "library book clubs", "domain.reading"
    ),
    InterestOption("interest.biographies", "biographies", "domain.reading"),
    InterestOption("interest.sourdough_baking", "sourdough baking", "domain.food"),
    InterestOption("interest.budget_cooking", "budget cooking", "domain.food"),
    InterestOption("interest.trying_food_trucks", "trying food trucks", "domain.food"),
    InterestOption(
        "interest.slow_cooker_recipes", "slow-cooker recipes", "domain.food"
    ),
    InterestOption(
        "interest.farmers_market_produce", "farmers market produce", "domain.food"
    ),
    InterestOption("interest.coffee_tasting", "coffee tasting", "domain.food"),
    InterestOption(
        "interest.hot_sauce_collecting", "hot sauce collecting", "domain.food"
    ),
    InterestOption("interest.vegetarian_recipes", "vegetarian recipes", "domain.food"),
    InterestOption("interest.making_preserves", "making preserves", "domain.food"),
    InterestOption("interest.model_trains", "model trains", "domain.transport"),
    InterestOption(
        "interest.car_repair_videos", "car repair videos", "domain.transport"
    ),
    InterestOption(
        "interest.public_transit_maps", "public transit maps", "domain.transport"
    ),
    InterestOption("interest.road_trips", "road trips", "domain.transport"),
    InterestOption(
        "interest.bicycle_maintenance", "bicycle maintenance", "domain.transport"
    ),
    InterestOption("interest.train_spotting", "train spotting", "domain.transport"),
    InterestOption("interest.motorcycle_rides", "motorcycle rides", "domain.transport"),
    InterestOption(
        "interest.aviation_documentaries", "aviation documentaries", "domain.transport"
    ),
    InterestOption(
        "interest.walking_new_routes", "walking new routes", "domain.transport"
    ),
    InterestOption("interest.postcards", "postcards", "domain.collecting"),
    InterestOption("interest.vintage_cameras", "vintage cameras", "domain.collecting"),
    InterestOption("interest.baseball_cards", "baseball cards", "domain.collecting"),
    InterestOption(
        "interest.rocks_and_minerals", "rocks and minerals", "domain.collecting"
    ),
    InterestOption("interest.old_cookbooks", "old cookbooks", "domain.collecting"),
    InterestOption("interest.concert_tickets", "concert tickets", "domain.collecting"),
    InterestOption(
        "interest.thrift_store_mugs", "thrift-store mugs", "domain.collecting"
    ),
    InterestOption("interest.stamps", "stamps", "domain.collecting"),
    InterestOption("interest.action_figures", "action figures", "domain.collecting"),
    InterestOption(
        "interest.food_bank_shifts", "food-bank shifts", "domain.volunteering"
    ),
    InterestOption(
        "interest.animal_shelter_help", "animal shelter help", "domain.volunteering"
    ),
    InterestOption(
        "interest.neighborhood_cleanups", "neighborhood cleanups", "domain.volunteering"
    ),
    InterestOption(
        "interest.community_garden_work", "community garden work", "domain.volunteering"
    ),
    InterestOption(
        "interest.mentoring_students", "mentoring students", "domain.volunteering"
    ),
    InterestOption(
        "interest.clothing_drives", "clothing drives", "domain.volunteering"
    ),
    InterestOption(
        "interest.disaster_relief_training",
        "disaster relief training",
        "domain.volunteering",
    ),
    InterestOption(
        "interest.library_volunteering", "library volunteering", "domain.volunteering"
    ),
    InterestOption(
        "interest.senior_center_visits", "senior-center visits", "domain.volunteering"
    ),
    InterestOption("interest.farmers_markets", "farmers markets", "domain.local_life"),
    InterestOption(
        "interest.neighborhood_festivals", "neighborhood festivals", "domain.local_life"
    ),
    InterestOption("interest.local_history", "local history", "domain.local_life"),
    InterestOption(
        "interest.city_council_meetings", "city council meetings", "domain.local_life"
    ),
    InterestOption(
        "interest.community_theater", "community theater", "domain.local_life"
    ),
    InterestOption("interest.coffee_shops", "coffee shops", "domain.local_life"),
    InterestOption("interest.street_fairs", "street fairs", "domain.local_life"),
    InterestOption("interest.open_mic_nights", "open mic nights", "domain.local_life"),
    InterestOption(
        "interest.local_restaurants", "local restaurants", "domain.local_life"
    ),
    InterestOption("interest.amateur_astronomy", "amateur astronomy", "domain.science"),
    InterestOption(
        "interest.citizen_science_surveys", "citizen science surveys", "domain.science"
    ),
    InterestOption("interest.weather_tracking", "weather tracking", "domain.science"),
    InterestOption(
        "interest.museum_science_talks", "museum science talks", "domain.science"
    ),
    InterestOption(
        "interest.microscope_projects", "microscope projects", "domain.science"
    ),
    InterestOption(
        "interest.nature_identification", "nature identification", "domain.science"
    ),
    InterestOption("interest.space_news", "space news", "domain.science"),
    InterestOption(
        "interest.chemistry_demonstrations",
        "chemistry demonstrations",
        "domain.science",
    ),
    InterestOption("interest.fossil_hunting", "fossil hunting", "domain.science"),
    InterestOption("interest.thrifting", "thrifting", "domain.everyday_low_cost"),
    InterestOption(
        "interest.free_museum_days", "free museum days", "domain.everyday_low_cost"
    ),
    InterestOption(
        "interest.public_library_events",
        "public library events",
        "domain.everyday_low_cost",
    ),
    InterestOption(
        "interest.coupon_hunting", "coupon hunting", "domain.everyday_low_cost"
    ),
    InterestOption(
        "interest.walking_errands", "walking errands", "domain.everyday_low_cost"
    ),
    InterestOption(
        "interest.free_online_courses",
        "free online courses",
        "domain.everyday_low_cost",
    ),
    InterestOption(
        "interest.people_watching", "people watching", "domain.everyday_low_cost"
    ),
    InterestOption(
        "interest.budget_travel_planning",
        "budget travel planning",
        "domain.everyday_low_cost",
    ),
    InterestOption(
        "interest.picnics_in_the_park",
        "picnics in the park",
        "domain.everyday_low_cost",
    ),
)
SECTOR_RELATED_DOMAINS = {
    "sector.food_and_hospitality": frozenset(("domain.food",)),
    "sector.transport_and_logistics": frozenset(("domain.transport",)),
    "sector.agriculture_and_environment": frozenset(
        ("domain.outdoors", "domain.science")
    ),
    "sector.healthcare_support": frozenset(("domain.science",)),
    "sector.healthcare_professional": frozenset(("domain.science",)),
    "sector.technology_and_digital": frozenset(("domain.science", "domain.games")),
    "sector.creative_media_and_culture": frozenset(("domain.crafts", "domain.music")),
    "sector.science_technical_and_professional": frozenset(("domain.science",)),
}
TROLL_MODIFIERS = (
    TrollModifier("troll.pedantic", "pedantic"),
    TrollModifier("troll.grievance_driven", "grievance-driven"),
    TrollModifier("troll.dismissive", "dismissive"),
    TrollModifier("troll.devils_advocate", "relentless devil's advocate"),
    TrollModifier("troll.status_seeking", "status-seeking"),
    TrollModifier("troll.suspicious", "suspicious"),
)
USERNAME_STYLES = (
    UsernameStyleOption(
        "phrase",
        "a short humorous phrase handle, e.g. pm_me_your_turtle, i_hate_mondays, legally_a_bird",
    ),
    UsernameStyleOption(
        "mashup",
        "two completely unrelated words mashed together, e.g. toaster_falcon, gravel_piano, sasquatch_ledger",
    ),
    UsernameStyleOption(
        "wordplay",
        "a pun or wordplay on a familiar phrase, e.g. ctrl_alt_defeat, thai_tanic, lug_wrench_romantic",
    ),
    UsernameStyleOption(
        "imperative",
        "an imperative verb + noun, e.g. adopt_a_duck, fear_the_soup, recycle_your_dad",
    ),
    UsernameStyleOption(
        "evocative",
        "a single evocative word + 2-4 digit number, e.g. moonlit_4821, harbor_77, verdigris_302",
    ),
)


_OCCUPATION_BY_ID = {o.id: o for o in OCCUPATIONS}
_OCCUPATIONS_BY_SECTOR = {
    s.id: tuple(o for o in OCCUPATIONS if o.sector == s.id) for s in SECTORS
}
_AGE_BY_ID = {b.id: b for b in AGE_BANDS}
_EDU_BY_ID = {e.id: e for e in EDUCATION_LEVELS}
_CONTEXT_BY_ID = {c.id: c for c in EMPLOYMENT_CONTEXTS}
_TRAIT_BY_ID = {t.id: t for t in TRAITS}
_TRAITS_BY_AXIS = {a: tuple(t for t in TRAITS if t.axis == a) for a in TRAIT_AXES}
_STYLE_BY_ID = {s.id: s for s in WRITING_STYLES}
_STYLES_BY_FAMILY = {
    f: tuple(s for s in WRITING_STYLES if s.family == f)
    for f in dict.fromkeys(s.family for s in WRITING_STYLES)
}
_INTEREST_BY_TEXT = {i.text: i for i in INTERESTS}


def _snapshot_counts(existing_users: Sequence[ExistingUserSnapshot]):
    sectors = {s.id: 0 for s in SECTORS}
    bands = {b.id: 0 for b in AGE_BANDS}
    levels = {e.id: 0 for e in EDUCATION_LEVELS}
    used: set[str] = set()
    seen: set[frozenset[str]] = set()
    for snapshot in existing_users:
        seed = (
            snapshot.persona_seed if isinstance(snapshot.persona_seed, Mapping) else {}
        )
        occupation_id = seed.get("occupation_id")
        if isinstance(occupation_id, str) and occupation_id in _OCCUPATION_BY_ID:
            used.add(occupation_id)
            sectors[_OCCUPATION_BY_ID[occupation_id].sector] += 1
        age_band_id = seed.get("age_band_id")
        band = _AGE_BY_ID.get(age_band_id) if isinstance(age_band_id, str) else None
        if band is not None:
            bands[band.id] += 1
        raw_level_id = seed.get("education_level_id")
        level_id: str | None = (
            raw_level_id
            if isinstance(raw_level_id, str) and raw_level_id in _EDU_BY_ID
            else None
        )
        if level_id is not None and level_id in levels:
            levels[level_id] += 1
        trait_ids = seed.get("trait_ids")
        if (
            isinstance(trait_ids, list | tuple)
            and len(trait_ids) == 4
            and all(isinstance(t, str) and t in _TRAIT_BY_ID for t in trait_ids)
        ):
            seen.add(frozenset(trait_ids))
    return sectors, bands, levels, used, seen


def _deficit_weights(
    targets: Sequence[float], observed: Sequence[int], count: int, total_existing: int
) -> tuple[list[float], list[float]]:
    denominator = max(1.0, total_existing + count)
    expected_total = total_existing + count
    deficits = [
        max(0.0, expected_total * target - observed[i])
        for i, target in enumerate(targets)
    ]
    weights = [
        target * (1.0 + deficits[i] / denominator) for i, target in enumerate(targets)
    ]
    return weights, deficits


def _quota(
    weights: Sequence[float],
    count: int,
    rng: random.Random,
    deficits: Sequence[float],
    caps: Sequence[int] | None = None,
) -> list[int]:
    total = sum(weights)
    raw = [(weight / total * count) if total else 0.0 for weight in weights]
    result = [math.floor(value) for value in raw]
    if caps is not None:
        result = [min(value, caps[i]) for i, value in enumerate(result)]
    remaining = count - sum(result)
    jitter = [rng.random() for _ in weights]
    while remaining > 0:
        candidates = [
            i for i in range(len(weights)) if caps is None or result[i] < caps[i]
        ]
        if not candidates:
            raise ValueError("allocation caps cannot satisfy count")
        candidates.sort(
            key=lambda i: (-(raw[i] - math.floor(raw[i])), -deficits[i], -jitter[i], i)
        )
        for index in candidates:
            if remaining == 0:
                break
            if caps is None or result[index] < caps[index]:
                result[index] += 1
                remaining -= 1
    return result


def _weighted_pick(items: Sequence, weights: Sequence[float], rng: random.Random):
    if not items:
        raise ValueError("no compatible options")
    total = sum(weights)
    cursor = rng.random() * total if total else 0.0
    for item, weight in zip(items, weights, strict=True):
        cursor -= weight
        if cursor < 0:
            return item
    return items[-1]


def _least_used(items: Sequence, uses: Mapping[str, int], rng: random.Random, key):
    minimum = min(uses.get(key(item), 0) for item in items)
    tied = [item for item in items if uses.get(key(item), 0) == minimum]
    return tied[rng.randrange(len(tied))]


def _context_weight(context_id: str, band_id: str) -> float:
    return CONTEXT_BASE_WEIGHTS[context_id] * CONTEXT_BAND_WEIGHT_MULTIPLIERS.get(
        band_id, {}
    ).get(context_id, 1.0)


def _card_band_compatible(card: OccupationOption, band: AgeBand) -> bool:
    """Whether any feasible age exists for this card inside the band."""
    if (card.min_age or 0) > band.high:
        return False
    if card.max_age is not None and card.max_age < band.low:
        return False
    return any(
        (_EDU_BY_ID[o.level_id].min_age or 0) <= band.high
        for o in card.education_options
    )


def _sector_compat_counts(band: AgeBand) -> list[float]:
    """Age-compatible card counts per sector, as quota weights."""
    return [
        float(
            sum(
                1
                for card in _OCCUPATIONS_BY_SECTOR[s.id]
                if _card_band_compatible(card, band)
            )
        )
        for s in SECTORS
    ]


def _draw_card(
    sector: str,
    band: AgeBand,
    bags: dict[str, list[OccupationOption]],
    uses: dict[str, int],
    rng: random.Random,
    unavailable: Collection[str] = frozenset(),
) -> OccupationOption:
    """Consume a compatible sector card, preferring historically unused cards.

    Cards from ``unavailable`` are historical assignments. They remain a
    fallback for compatibility or after the sector's fresh catalog cards have
    been consumed, but must not displace a compatible fresh card.
    """
    bag = bags[sector]

    def compatible(card: OccupationOption) -> bool:
        return _card_band_compatible(card, band)

    def pop_compatible() -> OccupationOption | None:
        # The bag is shuffled, so scan it in its existing draw order while
        # giving fresh cards a separate pass. This keeps seeded output
        # deterministic and prevents historical cards from winning merely
        # because of list ordering.
        for historical in (False, True):
            for index in range(len(bag) - 1, -1, -1):
                card = bag[index]
                if compatible(card) and ((card.id in unavailable) is historical):
                    card = bag.pop(index)
                    uses[card.id] = uses.get(card.id, 0) + 1
                    return card
        return None

    card = pop_compatible()
    if card is not None:
        return card
    if not bag:
        bag.extend(_OCCUPATIONS_BY_SECTOR[sector])
        rng.shuffle(bag)
        logger.debug("persona sector bag refill: %s", sector)
        card = pop_compatible()
        if card is not None:
            return card
    candidates = [
        card
        for card in _OCCUPATIONS_BY_SECTOR[sector]
        if compatible(card) and card.id not in unavailable
    ]
    if not candidates:
        candidates = [
            card for card in _OCCUPATIONS_BY_SECTOR[sector] if compatible(card)
        ]
    if not candidates:
        candidates = list(_OCCUPATIONS_BY_SECTOR[sector])
    # Compatibility pressure is rare (notably a veterinarian in the youngest band).
    card = _least_used(candidates, uses, rng, lambda item: item.id)
    uses[card.id] = uses.get(card.id, 0) + 1
    return card


def _draw_traits(
    bags: dict[str, list[TraitOption]], seen: set[frozenset[str]], rng: random.Random
):
    for _ in range(200):
        axes = rng.sample(TRAIT_AXES, 4)
        chosen = [_draw_trait(axis, bags, rng) for axis in axes]
        if not any(item.limitation for item in chosen):
            slot = rng.randrange(4)
            limitations = [
                item for item in _TRAITS_BY_AXIS[chosen[slot].axis] if item.limitation
            ]
            chosen[slot] = limitations[rng.randrange(len(limitations))]
        bad = any(
            frozenset((chosen[i].id, chosen[j].id)) in CONTRADICTING_TRAIT_PAIRS
            for i in range(4)
            for j in range(i + 1, 4)
        )
        combo = frozenset(item.id for item in chosen)
        if not bad and combo not in seen:
            seen.add(combo)
            return tuple(item.id for item in chosen), tuple(
                item.text for item in chosen
            )
    raise RuntimeError("unable to construct a unique trait combination")


def _draw_trait(axis: str, bags: dict[str, list[TraitOption]], rng: random.Random):
    if not bags[axis]:
        bags[axis] = list(_TRAITS_BY_AXIS[axis])
        rng.shuffle(bags[axis])
    return bags[axis].pop()


def _style_bag(rng: random.Random) -> list[WritingStyleOption]:
    families = list(_STYLES_BY_FAMILY)
    rng.shuffle(families)
    grouped = {family: list(_STYLES_BY_FAMILY[family]) for family in families}
    for family in families:
        rng.shuffle(grouped[family])
    result = []
    while any(grouped[family] for family in families):
        for family in families:
            if grouped[family]:
                result.append(grouped[family].pop())
    return result


def build_persona_assignments(
    count: int,
    troll_count: int,
    existing_users: Sequence[ExistingUserSnapshot] | None,
    rng: random.Random,
) -> tuple[PersonaAssignment, ...]:
    """Build a deterministic assignment matrix from immutable catalogs.

    Cards are consumed from per-sector bags. Historical occupation cards are
    skipped while a compatible fresh card remains in that sector's catalog.
    The only path that can repeat a card before its sector is exhausted is
    compatibility pressure: if no card left in the bag fits an age band, a
    least-used compatible card is reused.
    """
    if not 1 <= count <= 500:
        raise ValueError("count must be between 1 and 500")
    if not 0 <= troll_count <= count:
        raise ValueError("troll_count must be between 0 and count")
    snapshots = tuple(existing_users or ())
    sector_counts, band_counts, level_counts, used_occupations, seen_traits = (
        _snapshot_counts(snapshots)
    )
    total_existing = len(snapshots)
    age_weights, age_deficits = _deficit_weights(
        [band.target for band in AGE_BANDS],
        [band_counts[band.id] for band in AGE_BANDS],
        count,
        total_existing,
    )
    band_quota = _quota(age_weights, count, rng, age_deficits)
    sector_weights, sector_deficits = _deficit_weights(
        [1 / len(SECTORS)] * len(SECTORS),
        [sector_counts[s.id] for s in SECTORS],
        count,
        total_existing,
    )
    # Sector seats are allocated per age band, weighted by how many of each
    # sector's cards that band can actually use. Young bands therefore skew
    # toward enterable occupations instead of pairing with professions whose
    # credentials cannot exist at that age. The global per-sector cap from
    # the single-pool allocator is preserved by tracking running totals.
    global_sector_cap = (
        count
        if count < 20
        else max(math.floor(0.2 * count), math.ceil(count / len(SECTORS)))
    )
    sector_running = {sector.id: 0 for sector in SECTORS}
    specs = []
    for band, seats in zip(AGE_BANDS, band_quota, strict=True):
        if not seats:
            continue
        compat = _sector_compat_counts(band)
        weights = [
            weight * compatible_cards
            for weight, compatible_cards in zip(sector_weights, compat, strict=True)
        ]
        if not any(weights):
            weights = list(sector_weights)
        caps = [
            # Zero-compat sectors are hard-excluded so no remainder seat can
            # ever pair an impossible (band, sector) combination.
            0
            if compatible_cards == 0
            else max(0, global_sector_cap - sector_running[sector.id])
            for sector, compatible_cards in zip(SECTORS, compat, strict=True)
        ]
        allocation = _quota(weights, seats, rng, sector_deficits, caps)
        band_sectors = []
        for sector, sector_seats in zip(SECTORS, allocation, strict=True):
            sector_running[sector.id] += sector_seats
            band_sectors.extend([sector.id] * sector_seats)
        rng.shuffle(band_sectors)
        specs.extend((band, sector_id) for sector_id in band_sectors)
    # Repair passes over the (band, sector) pairing. Both preserve band
    # quotas exactly, keep every pairing age-compatible, and draw no rng.
    # Coverage: every sector reachable in some band gets at least one seat,
    # funded from the largest compatible donor row. Balance: no sector keeps
    # more seats than the single-pool allocator would have allowed.
    sector_ids = [sector.id for sector in SECTORS]
    compat_by_band = {band.id: _sector_compat_counts(band) for band in AGE_BANDS}
    sector_totals: dict[str, int] = {}
    for _, sector_id in specs:
        sector_totals[sector_id] = sector_totals.get(sector_id, 0) + 1
    sector_limit = max(1, math.ceil(count / len(SECTORS)))

    def swap(index: int, target_sector: str) -> None:
        donor_sector = specs[index][1]
        specs[index] = (specs[index][0], target_sector)
        sector_totals[donor_sector] -= 1
        sector_totals[target_sector] = sector_totals.get(target_sector, 0) + 1

    for position, sector_id in enumerate(sector_ids):
        if sector_totals.get(sector_id, 0):
            continue
        donor = None
        donor_total = 1
        for index, (band, row_sector) in enumerate(specs):
            if sector_totals[row_sector] <= donor_total:
                continue
            if compat_by_band[band.id][position] > 0:
                donor, donor_total = index, sector_totals[row_sector]
        if donor is not None:
            swap(donor, sector_id)
    while sum(max(0, total - sector_limit) for total in sector_totals.values()):
        swap_index = swap_target = None
        for index, (band, row_sector) in enumerate(specs):
            if sector_totals[row_sector] <= sector_limit:
                continue
            compat = compat_by_band[band.id]
            for position, candidate_id in enumerate(sector_ids):
                if (
                    compat[position] > 0
                    and sector_totals.get(candidate_id, 0) < sector_limit
                ):
                    swap_index, swap_target = index, candidate_id
                    break
            if swap_index is not None:
                break
        if swap_index is None or swap_target is None:
            break
        swap(swap_index, swap_target)
    troll_indices = set(rng.sample(range(count), troll_count)) if troll_count else set()
    occupation_bags = {}
    for sector in SECTORS:
        cards = list(_OCCUPATIONS_BY_SECTOR[sector.id])
        rng.shuffle(cards)
        occupation_bags[sector.id] = [
            card for card in cards if card.id not in used_occupations
        ] + [card for card in cards if card.id in used_occupations]
    occupation_uses: dict[str, int] = {}
    trait_bags = {axis: list(_TRAITS_BY_AXIS[axis]) for axis in TRAIT_AXES}
    for axis in TRAIT_AXES:
        rng.shuffle(trait_bags[axis])
    styles = _style_bag(rng)
    usernames = list(USERNAME_STYLES)
    rng.shuffle(usernames)
    trolls = list(TROLL_MODIFIERS)
    rng.shuffle(trolls)
    context_uses = {context.id: 0 for context in EMPLOYMENT_CONTEXTS}
    level_uses = dict(level_counts)
    education_uses: dict[str, int] = {}
    interest_uses: dict[str, int] = dict.fromkeys(INTEREST_DOMAINS, 0)
    interest_bags: dict[str, list[InterestOption]] = {}
    for domain in INTEREST_DOMAINS:
        cards = [item for item in INTERESTS if item.domain == domain]
        rng.shuffle(cards)
        interest_bags[domain] = cards
    rows = []
    for row_index, (band, sector) in enumerate(specs):
        card = _draw_card(
            sector, band, occupation_bags, occupation_uses, rng, used_occupations
        )
        contexts = []
        for context_id in card.allowed_contexts:
            context = _CONTEXT_BY_ID[context_id]
            if context_id == "context.current_student" and not any(
                option.level_id == "education.current_student"
                for option in card.education_options
            ):
                continue
            low = max(band.low, card.min_age or band.low, context.min_age or band.low)
            high = min(
                band.high, context.max_age or band.high, card.max_age or band.high
            )
            if low <= high:
                contexts.append(context)
        context = _weighted_pick(
            contexts,
            [
                # Mild anti-clumping only: strong dampening flattened the
                # intended per-band proportions toward uniform.
                _context_weight(item.id, band.id) / (1 + 0.25 * context_uses[item.id])
                for item in contexts
            ],
            rng,
        )
        low = max(band.low, card.min_age or band.low, context.min_age or band.low)
        high = min(band.high, context.max_age or band.high, card.max_age or band.high)
        levels = []
        for option in card.education_options:
            level = _EDU_BY_ID[option.level_id]
            if (level.min_age or 0) > high:
                continue
            # The current-student level and context require each other; the
            # student level never rides along with another context (plan
            # coupling rule, including retired/between-jobs exclusions).
            if (level.id == "education.current_student") != (
                context.id == "context.current_student"
            ):
                continue
            levels.append(level)
        if count >= 20:
            cap = math.ceil(0.3 * count)
            under_cap = [level for level in levels if level_uses.get(level.id, 0) < cap]
            if under_cap:
                levels = under_cap
            else:
                # Compatibility-forced exception (plan: "unless compatibility
                # makes that impossible"): choose the least-used level.
                levels = [min(levels, key=lambda lv: (level_uses.get(lv.id, 0), lv.id))]
        level_weights = [
            EDUCATION_LEVEL_TARGETS[level.id]
            * (
                1
                + max(
                    0,
                    (sum(level_uses.values()) + count)
                    * EDUCATION_LEVEL_TARGETS[level.id]
                    - level_uses.get(level.id, 0),
                )
            )
            / (1 + level_uses.get(level.id, 0))
            for level in levels
        ]
        level = _weighted_pick(levels, level_weights, rng)
        low = max(low, level.min_age or low)
        if context.id == "context.current_student":
            # Students cluster at the young end of their feasible window:
            # undergrads at 18-22, returning students just above their floor.
            age = low + int((high + 1 - low) * rng.random() ** 4.0)
        else:
            age = rng.randrange(low, high + 1)
        options = [
            option for option in card.education_options if option.level_id == level.id
        ]
        option = _least_used(options, education_uses, rng, lambda item: item.text)
        education_uses[option.text] = education_uses.get(option.text, 0) + 1
        level_uses[level.id] = level_uses.get(level.id, 0) + 1
        context_uses[context.id] += 1
        trait_ids, trait_texts = _draw_traits(trait_bags, seen_traits, rng)
        if not styles:
            styles = _style_bag(rng)
        style = styles.pop()
        related = SECTOR_RELATED_DOMAINS.get(sector, frozenset())
        first_domain: str = _least_used(
            [d for d in INTEREST_DOMAINS if d not in related],
            interest_uses,
            rng,
            lambda d: d,
        )
        second_domain: str = _least_used(
            [d for d in INTEREST_DOMAINS if d != first_domain],
            interest_uses,
            rng,
            lambda d: d,
        )
        seeds = []
        for domain in (first_domain, second_domain):
            if not interest_bags[domain]:
                interest_bags[domain] = [
                    item for item in INTERESTS if item.domain == domain
                ]
                rng.shuffle(interest_bags[domain])
            seeds.append(interest_bags[domain].pop().text)
            interest_uses[domain] += 1
        if row_index in troll_indices:
            if not trolls:
                trolls = list(TROLL_MODIFIERS)
                rng.shuffle(trolls)
            troll = trolls.pop()
        else:
            troll = None
        if not usernames:
            usernames = list(USERNAME_STYLES)
            rng.shuffle(usernames)
        username = usernames.pop()
        rows.append(
            PersonaAssignment(
                "",
                age,
                band.id,
                card.id,
                card.label,
                card.sector,
                context.id,
                context.label,
                level.id,
                option.text,
                trait_ids,
                trait_texts,
                style.id,
                style.text,
                tuple(seeds),
                troll.id if troll else None,
                troll.text if troll else None,
                username.text,
            )
        )
    rng.shuffle(rows)
    return tuple(
        PersonaAssignment(
            f"a{i}",
            row.age,
            row.age_band_id,
            row.occupation_id,
            row.occupation,
            row.occupation_sector,
            row.employment_context_id,
            row.employment_context,
            row.education_level_id,
            row.education,
            row.trait_ids,
            row.traits,
            row.writing_style_id,
            row.writing_style,
            row.interest_seeds,
            row.troll_modifier_id,
            row.troll_modifier,
            row.username_style,
        )
        for i, row in enumerate(rows, 1)
    )


def validate_assignment(assignment: PersonaAssignment) -> tuple[str, ...]:
    """Return one clear message per violated assignment invariant."""
    errors: list[str] = []
    if not assignment.id:
        errors.append("id must be non-empty")
    band = _AGE_BY_ID.get(assignment.age_band_id)
    if band is None:
        errors.append("unknown age band id")
    elif not band.low <= assignment.age <= band.high:
        errors.append("age is outside its age band")
    card = _OCCUPATION_BY_ID.get(assignment.occupation_id)
    if card is None:
        errors.append("unknown occupation id")
    else:
        if assignment.occupation != card.label:
            errors.append("occupation text does not match catalog")
        if assignment.occupation_sector != card.sector:
            errors.append("occupation sector does not match catalog")
        if len(assignment.occupation) > 100:
            errors.append("occupation text exceeds 100 characters")
        if card.min_age is not None and assignment.age < card.min_age:
            errors.append("age is below occupation minimum")
        if card.max_age is not None and assignment.age > card.max_age:
            errors.append("age is above occupation maximum")
    context = _CONTEXT_BY_ID.get(assignment.employment_context_id)
    if context is None:
        errors.append("unknown employment context id")
    else:
        if assignment.employment_context != context.label:
            errors.append("employment context text does not match catalog")
        if context.min_age is not None and assignment.age < context.min_age:
            errors.append("age is below employment context minimum")
        if context.max_age is not None and assignment.age > context.max_age:
            errors.append("age is above employment context maximum")
        if card is not None and context.id not in card.allowed_contexts:
            errors.append("occupation does not allow employment context")
    level = _EDU_BY_ID.get(assignment.education_level_id)
    if level is None:
        errors.append("unknown education level id")
    else:
        if level.min_age is not None and assignment.age < level.min_age:
            errors.append("age is below education minimum")
        if len(assignment.education) > 100:
            errors.append("education text exceeds 100 characters")
        if card is not None and not any(
            option.level_id == level.id and option.text == assignment.education
            for option in card.education_options
        ):
            errors.append("education text is not an option for occupation level")
    if context is not None and level is not None:
        if (context.id == "context.current_student") != (
            level.id == "education.current_student"
        ):
            # Covers the plan's named exclusions too: the student level never
            # rides along with retired, between-jobs, or any other context.
            errors.append("current-student context and education must match")
    if len(assignment.trait_ids) != 4 or len(assignment.traits) != 4:
        errors.append("exactly four traits are required")
    else:
        trait_options = [
            _TRAIT_BY_ID.get(trait_id) for trait_id in assignment.trait_ids
        ]
        if any(item is None for item in trait_options):
            errors.append("trait id is not in catalog")
        elif len(set(assignment.trait_ids)) != 4:
            errors.append("trait ids must be distinct")
        else:
            present_trait_options = [item for item in trait_options if item is not None]
            if len({item.axis for item in present_trait_options}) != 4:
                errors.append("traits must come from four distinct axes")
            if not any(item.limitation for item in present_trait_options):
                errors.append("traits require at least one limitation")
            if any(
                frozenset((assignment.trait_ids[i], assignment.trait_ids[j]))
                in CONTRADICTING_TRAIT_PAIRS
                for i in range(4)
                for j in range(i + 1, 4)
            ):
                errors.append("traits contain a contradicting pair")
            if tuple(item.text for item in present_trait_options) != assignment.traits:
                errors.append("trait text does not match trait ids")
    style = _STYLE_BY_ID.get(assignment.writing_style_id)
    if style is None:
        errors.append("unknown writing style id")
    elif style.text != assignment.writing_style:
        errors.append("writing style text does not match catalog")
    if len(assignment.interest_seeds) != 2:
        errors.append("exactly two interest seeds are required")
    else:
        interests = [_INTEREST_BY_TEXT.get(text) for text in assignment.interest_seeds]
        if any(item is None for item in interests):
            errors.append("interest seed is not in catalog")
        else:
            present_interests = [item for item in interests if item is not None]
            if present_interests[0].domain == present_interests[1].domain:
                errors.append("interest seeds must have distinct domains")
            elif present_interests[0].domain in SECTOR_RELATED_DOMAINS.get(
                assignment.occupation_sector, frozenset()
            ) and present_interests[1].domain in SECTOR_RELATED_DOMAINS.get(
                assignment.occupation_sector, frozenset()
            ):
                errors.append(
                    "at least one interest domain must be unrelated to occupation sector"
                )
    if (assignment.troll_modifier_id is None) != (assignment.troll_modifier is None):
        errors.append("troll modifier id and text must both be present or absent")
    elif assignment.troll_modifier_id is not None:
        troll = next(
            (
                item
                for item in TROLL_MODIFIERS
                if item.id == assignment.troll_modifier_id
            ),
            None,
        )
        if troll is None or troll.text != assignment.troll_modifier:
            errors.append("troll modifier does not match catalog")
    if assignment.username_style not in tuple(item.text for item in USERNAME_STYLES):
        errors.append("username style is not in catalog")
    return tuple(errors)
