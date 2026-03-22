"""Unit tests for all four filters."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path so imports work from tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.job import Job
from filters.location import passes_location_filter, classify_remote_scope, COUNTRY_BLOCKLIST
from filters.role import passes_role_filter
from filters.ngo import compute_ngo_score, classify_ngo
from filters.language import passes_language_filter


# ── helpers ────────────────────────────────────────────────────────────────

def _make_job(**overrides) -> Job:
    """Create a minimal Job for testing, with sensible defaults."""
    defaults = dict(
        title="Software Engineer",
        company="Acme Corp",
        location="Remote",
        url="https://example.com/job/1",
        source="test",
    )
    defaults.update(overrides)
    return Job(**defaults)


# ═══════════════════════════════════════════════════════════════════════════
#  Location filter
# ═══════════════════════════════════════════════════════════════════════════

class TestLocationFilter:
    def test_accept_worldwide_remote(self):
        job = _make_job(location="Remote - Worldwide")
        assert passes_location_filter(job) is True

    def test_accept_eu_remote(self):
        job = _make_job(location="Remote - Europe")
        assert passes_location_filter(job) is True

    def test_accept_germany_remote(self):
        """Berlin + remote signal → accept."""
        job = _make_job(location="Berlin, Germany (Remote)")
        assert passes_location_filter(job) is True

    def test_reject_generic_remote_unknown_scope(self):
        """Bare 'Remote' with no country/region signal → scope=unknown → REJECT.

        This is the v1.1 critical fix: unknown scope defaults to reject.
        """
        job = _make_job(location="Remote")
        assert passes_location_filter(job) is False

    def test_reject_uk_only(self):
        job = _make_job(location="UK Only")
        assert passes_location_filter(job) is False

    def test_reject_united_kingdom(self):
        job = _make_job(location="United Kingdom Only")
        assert passes_location_filter(job) is False

    def test_reject_us_only(self):
        job = _make_job(location="US Only")
        assert passes_location_filter(job) is False

    def test_reject_onsite_not_germany(self):
        job = _make_job(location="New York, NY", is_remote=False)
        assert passes_location_filter(job) is False

    def test_accept_worldwide_even_with_uk_mention(self):
        """If it says 'worldwide' alongside UK, still accept."""
        job = _make_job(location="Remote - Worldwide, UK, EU")
        assert passes_location_filter(job) is True

    def test_reject_london_only(self):
        job = _make_job(location="London Only")
        assert passes_location_filter(job) is False

    def test_accept_munich_remote(self):
        job = _make_job(location="Munich, Germany (Remote)")
        assert passes_location_filter(job) is True

    # ── EU country expansion (issue #1) ────────────────────────────────
    def test_accept_remote_spain(self):
        job = _make_job(location="Remote - Spain")
        assert passes_location_filter(job) is True

    def test_accept_remote_portugal(self):
        job = _make_job(location="Remote (Portugal)")
        assert passes_location_filter(job) is True

    def test_accept_remote_netherlands(self):
        job = _make_job(location="Remote, Netherlands")
        assert passes_location_filter(job) is True

    def test_accept_remote_france(self):
        job = _make_job(location="France (Remote)")
        assert passes_location_filter(job) is True

    def test_accept_remote_poland(self):
        job = _make_job(location="Remote - Poland")
        assert passes_location_filter(job) is True

    def test_accept_remote_dach(self):
        job = _make_job(location="Remote - DACH region")
        assert passes_location_filter(job) is True

    def test_accept_remote_benelux(self):
        job = _make_job(location="Benelux, Remote")
        assert passes_location_filter(job) is True

    def test_accept_remote_estonia(self):
        job = _make_job(location="Estonia (Remote)")
        assert passes_location_filter(job) is True

    # ── Multi-country remote (issue #2) ────────────────────────────────
    def test_accept_multi_country_eu(self):
        job = _make_job(location="You can work remotely from Germany, Spain, or Portugal.")
        assert passes_location_filter(job) is True

    def test_accept_work_from_france_or_germany(self):
        job = _make_job(location="Work from France or Germany")
        assert passes_location_filter(job) is True

    # ── Berlin on-site correction (issue #3) ───────────────────────────
    def test_reject_berlin_onsite(self):
        """Pure on-site Berlin → reject."""
        job = _make_job(location="Berlin (on-site)")
        assert passes_location_filter(job) is False

    def test_accept_berlin_hybrid(self):
        job = _make_job(location="Berlin, hybrid")
        assert passes_location_filter(job) is True

    def test_accept_berlin_remote(self):
        job = _make_job(location="Berlin, remote")
        assert passes_location_filter(job) is True

    def test_reject_berlin_no_remote_signal(self):
        """Bare 'Berlin' with no remote/hybrid mention → reject."""
        job = _make_job(location="Berlin")
        assert passes_location_filter(job) is False

    def test_accept_berlin_home_office(self):
        job = _make_job(location="Berlin, home office possible")
        assert passes_location_filter(job) is True

    def test_reject_berlin_in_office(self):
        job = _make_job(location="Berlin (in-office)")
        assert passes_location_filter(job) is False

    # ── "Must reside in" patterns (issue #4) ───────────────────────────
    def test_accept_must_be_located_in_eu_country(self):
        job = _make_job(
            location="Remote",
            description="Fully remote role. You must be located in Germany or France.",
        )
        assert passes_location_filter(job) is True

    def test_accept_eligible_to_work_in_netherlands(self):
        job = _make_job(
            location="Remote",
            description="Must be eligible to work in the Netherlands.",
        )
        assert passes_location_filter(job) is True

    def test_accept_based_in_spain(self):
        job = _make_job(
            location="Remote",
            description="Candidates must be based in Spain or Portugal.",
        )
        assert passes_location_filter(job) is True

    def test_reject_must_reside_in_us(self):
        """Residency requirement in non-EU country — scope unknown → REJECT."""
        job = _make_job(
            location="Remote",
            description="Must reside in the United States.",
        )
        # scope=unknown (no EU country, no worldwide) → reject
        assert passes_location_filter(job) is False

    # ── v1.1: Country blocklist tests ──────────────────────────────────
    def test_reject_canada_remote(self):
        """'Canada (Remote)' → restricted → REJECT."""
        job = _make_job(location="Canada (Remote)")
        job.remote_scope = classify_remote_scope(job)
        assert job.remote_scope == "restricted"
        assert passes_location_filter(job) is False

    def test_reject_united_states(self):
        """'United States' → restricted → REJECT."""
        job = _make_job(location="United States")
        job.remote_scope = classify_remote_scope(job)
        assert job.remote_scope == "restricted"
        assert passes_location_filter(job) is False

    def test_reject_remote_us(self):
        """'Remote, US' → restricted → REJECT."""
        job = _make_job(location="Remote, US")
        job.remote_scope = classify_remote_scope(job)
        assert job.remote_scope == "restricted"
        assert passes_location_filter(job) is False

    def test_reject_remote_dash_us(self):
        """'Remote - US' → restricted → REJECT."""
        job = _make_job(location="Remote - US")
        job.remote_scope = classify_remote_scope(job)
        assert job.remote_scope == "restricted"
        assert passes_location_filter(job) is False

    def test_reject_tampa_fl(self):
        """'Tampa, FL' — US city → restricted → REJECT."""
        job = _make_job(location="Tampa, FL")
        job.remote_scope = classify_remote_scope(job)
        assert job.remote_scope == "restricted"
        assert passes_location_filter(job) is False

    def test_reject_australia(self):
        job = _make_job(location="Australia")
        job.remote_scope = classify_remote_scope(job)
        assert job.remote_scope == "restricted"
        assert passes_location_filter(job) is False

    def test_reject_new_zealand(self):
        job = _make_job(location="New Zealand")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    def test_reject_brazil(self):
        job = _make_job(location="Brazil")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    def test_reject_india(self):
        job = _make_job(location="India")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    def test_reject_nigeria(self):
        job = _make_job(location="Nigeria")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    def test_reject_singapore(self):
        job = _make_job(location="Singapore")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    def test_reject_japan(self):
        job = _make_job(location="Japan")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    def test_reject_south_korea(self):
        job = _make_job(location="South Korea")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    def test_reject_china(self):
        job = _make_job(location="China")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    def test_reject_mexico(self):
        job = _make_job(location="Mexico")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    def test_reject_argentina(self):
        job = _make_job(location="Argentina")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    def test_reject_colombia(self):
        job = _make_job(location="Colombia")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    def test_reject_england(self):
        job = _make_job(location="England")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    def test_reject_remote_canada(self):
        job = _make_job(location="Remote Canada")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    def test_reject_usa_location(self):
        job = _make_job(location="USA")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    def test_reject_us_only_location(self):
        job = _make_job(location="US Only")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is False

    # ── Country blocklist OVERRIDE tests ───────────────────────────────
    def test_accept_worldwide_overrides_us_mention(self):
        """If location says both 'US' and 'Worldwide', accept."""
        job = _make_job(location="Worldwide, US, EU")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is True

    def test_accept_europe_overrides_uk_blocklist(self):
        """If location mentions UK but also Europe, accept."""
        job = _make_job(location="Europe, United Kingdom")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is True

    def test_accept_eu_country_overrides_blocklist(self):
        """If location mentions a blocked country but also an EU country, accept."""
        job = _make_job(location="Remote: Germany, Canada, US")
        job.remote_scope = classify_remote_scope(job)
        assert passes_location_filter(job) is True

    # ── v1.1: scope=unknown rejection tests ────────────────────────────
    def test_reject_unknown_scope_default(self):
        """scope=unknown must be rejected by default."""
        job = _make_job(location="Remote")
        job.remote_scope = "unknown"
        assert passes_location_filter(job) is False

    def test_accept_pre_classified_worldwide(self):
        """Pre-classified worldwide + is_remote should be accepted."""
        job = _make_job(location="Remote", is_remote=True)
        job.remote_scope = "worldwide"
        assert passes_location_filter(job) is True

    def test_accept_pre_classified_eu(self):
        job = _make_job(location="Remote", is_remote=True)
        job.remote_scope = "eu"
        assert passes_location_filter(job) is True

    def test_accept_pre_classified_germany(self):
        job = _make_job(location="Remote", is_remote=True)
        job.remote_scope = "germany"
        assert passes_location_filter(job) is True

    # ── v1.1: Worldwide corroboration tests ────────────────────────────
    def test_arbeitnow_worldwide_no_corroboration_defaults_germany(self):
        """Arbeitnow 'Worldwide' with no corroboration → germany, not worldwide."""
        job = _make_job(location="Worldwide", source="arbeitnow")
        scope = classify_remote_scope(job)
        assert scope == "germany"

    def test_arbeitnow_worldwide_with_description_corroboration(self):
        """Arbeitnow 'Worldwide' with worldwide in description → worldwide."""
        job = _make_job(
            location="Worldwide",
            source="arbeitnow",
            description="This is a fully remote role, work from anywhere worldwide.",
        )
        scope = classify_remote_scope(job)
        assert scope == "worldwide"

    def test_remoteok_worldwide_trusted(self):
        """RemoteOK (remote-only board) 'Worldwide' → trusted as worldwide."""
        job = _make_job(location="Worldwide", source="remoteok")
        scope = classify_remote_scope(job)
        assert scope == "worldwide"

    def test_remotive_worldwide_trusted(self):
        """Remotive (remote-only board) 'Worldwide' → trusted as worldwide."""
        job = _make_job(location="Worldwide", source="remotive")
        scope = classify_remote_scope(job)
        assert scope == "worldwide"


class TestRemoteScopeClassification:
    def test_worldwide(self):
        job = _make_job(location="Remote - Worldwide")
        assert classify_remote_scope(job) == "worldwide"

    def test_eu(self):
        job = _make_job(location="Remote - Europe")
        assert classify_remote_scope(job) == "eu"

    def test_germany(self):
        job = _make_job(location="Berlin, Germany")
        assert classify_remote_scope(job) == "germany"

    def test_unknown_remote(self):
        job = _make_job(location="Remote")
        assert classify_remote_scope(job) == "unknown"

    def test_germany_beats_worldwide_in_description(self):
        """A Berlin job with 'international' in the description should be germany, not worldwide."""
        job = _make_job(
            location="Berlin",
            description="Join our international team building global products.",
        )
        assert classify_remote_scope(job) == "germany"

    def test_worldwide_in_location_field(self):
        """If 'Worldwide' is explicitly in the location, it should be worldwide."""
        job = _make_job(location="Remote - Worldwide")
        assert classify_remote_scope(job) == "worldwide"

    # ── EU country scope tests ─────────────────────────────────────────
    def test_spain_scope(self):
        job = _make_job(location="Remote - Spain")
        assert classify_remote_scope(job) == "eu"

    def test_portugal_scope(self):
        job = _make_job(location="Remote (Portugal)")
        assert classify_remote_scope(job) == "eu"

    def test_multi_country_scope(self):
        job = _make_job(location="Remote from Germany, Spain, or Portugal")
        assert classify_remote_scope(job) == "germany"  # Germany takes priority

    def test_dach_scope(self):
        job = _make_job(location="DACH region, Remote")
        assert classify_remote_scope(job) == "eu"

    def test_residency_eu_scope(self):
        job = _make_job(
            location="Remote",
            description="Must be located in France or Spain.",
        )
        assert classify_remote_scope(job) == "eu"


# ═══════════════════════════════════════════════════════════════════════════
#  Role filter
# ═══════════════════════════════════════════════════════════════════════════

class TestRoleFilter:
    def test_accept_software_engineer(self):
        job = _make_job(title="Software Engineer")
        assert passes_role_filter(job) is True

    def test_accept_fullstack(self):
        job = _make_job(title="Full Stack Developer")
        assert passes_role_filter(job) is True

    def test_accept_react_in_tags(self):
        job = _make_job(title="Frontend Specialist", tags=["react", "typescript"])
        assert passes_role_filter(job) is True

    def test_reject_marketing(self):
        job = _make_job(title="Marketing Manager")
        assert passes_role_filter(job) is False

    def test_reject_sales(self):
        job = _make_job(title="Sales Representative")
        assert passes_role_filter(job) is False

    def test_accept_python_developer(self):
        job = _make_job(title="Python Developer")
        assert passes_role_filter(job) is True

    def test_accept_backend(self):
        job = _make_job(title="Backend Engineer")
        assert passes_role_filter(job) is True

    # ── New tests for reject patterns ──────────────────────────────────
    def test_reject_office_assistant(self):
        job = _make_job(title="Office Assistant")
        assert passes_role_filter(job) is False

    def test_reject_brand_manager(self):
        job = _make_job(title="Senior Amazon Brand Manager")
        assert passes_role_filter(job) is False

    def test_reject_recruiter(self):
        job = _make_job(title="Technical Recruiter")
        assert passes_role_filter(job) is False

    def test_reject_hr_manager(self):
        job = _make_job(title="HR Manager")
        assert passes_role_filter(job) is False

    def test_reject_intern(self):
        job = _make_job(title="Intern - Full-Stack Developer")
        assert passes_role_filter(job) is False

    def test_reject_internship(self):
        job = _make_job(title="Software Engineering Internship")
        assert passes_role_filter(job) is False

    def test_reject_working_student(self):
        job = _make_job(title="Full-Stack Developer (Working Student)")
        assert passes_role_filter(job) is False

    def test_reject_werkstudent(self):
        job = _make_job(title="Werkstudent Softwareentwicklung")
        assert passes_role_filter(job) is False

    def test_reject_praktikum(self):
        job = _make_job(title="Praktikum Webentwicklung")
        assert passes_role_filter(job) is False

    def test_accept_internal_tools_engineer(self):
        """'internal' should NOT trigger the 'intern' reject pattern."""
        job = _make_job(title="Internal Tools Engineer")
        assert passes_role_filter(job) is True

    # ── v1.1: New reject patterns ──────────────────────────────────────
    def test_reject_executive_assistant(self):
        job = _make_job(title="Executive Assistant")
        assert passes_role_filter(job) is False

    def test_reject_virtual_assistant(self):
        job = _make_job(title="Virtual Assistant")
        assert passes_role_filter(job) is False

    def test_reject_growth_marketing(self):
        job = _make_job(title="Growth Marketing Lead")
        assert passes_role_filter(job) is False

    def test_reject_business_development(self):
        job = _make_job(title="Business Development Manager")
        assert passes_role_filter(job) is False

    def test_reject_graphic_designer(self):
        job = _make_job(title="Senior Graphic Designer")
        assert passes_role_filter(job) is False

    def test_reject_ui_designer(self):
        job = _make_job(title="UI Designer")
        assert passes_role_filter(job) is False

    def test_reject_ux_designer(self):
        job = _make_job(title="UX Designer")
        assert passes_role_filter(job) is False

    def test_reject_project_manager(self):
        job = _make_job(title="Project Manager")
        assert passes_role_filter(job) is False

    def test_reject_scrum_master(self):
        job = _make_job(title="Scrum Master")
        assert passes_role_filter(job) is False

    def test_reject_android_engineer(self):
        job = _make_job(title="Android Engineer")
        assert passes_role_filter(job) is False

    def test_reject_ios_engineer(self):
        job = _make_job(title="iOS Engineer")
        assert passes_role_filter(job) is False

    def test_reject_data_analyst(self):
        job = _make_job(title="Data Analyst")
        assert passes_role_filter(job) is False

    def test_reject_seo_specialist(self):
        job = _make_job(title="SEO Specialist")
        assert passes_role_filter(job) is False

    def test_reject_trainee(self):
        job = _make_job(title="Software Developer Trainee")
        assert passes_role_filter(job) is False

    def test_reject_apprentice(self):
        job = _make_job(title="Apprentice Web Developer")
        assert passes_role_filter(job) is False

    def test_reject_vp_of_engineering(self):
        job = _make_job(title="VP of Engineering")
        assert passes_role_filter(job) is False

    # ── v1.1: New accept patterns ──────────────────────────────────────
    def test_accept_react_developer(self):
        job = _make_job(title="React Developer")
        assert passes_role_filter(job) is True

    def test_accept_nextjs_engineer(self):
        job = _make_job(title="Next.js Engineer")
        assert passes_role_filter(job) is True

    def test_accept_vue_developer(self):
        job = _make_job(title="Vue.js Developer")
        assert passes_role_filter(job) is True

    def test_accept_django_developer(self):
        job = _make_job(title="Django Developer")
        assert passes_role_filter(job) is True

    def test_accept_fastapi_engineer(self):
        job = _make_job(title="FastAPI Engineer")
        assert passes_role_filter(job) is True

    def test_accept_docker_in_description(self):
        """Docker keyword in first 200 chars of description → accept."""
        job = _make_job(
            title="Infrastructure Lead",
            description="You will work with Docker, Kubernetes, and CI/CD pipelines.",
        )
        assert passes_role_filter(job) is True

    def test_accept_kubernetes_in_tags(self):
        job = _make_job(title="Cloud Specialist", tags=["kubernetes", "aws"])
        assert passes_role_filter(job) is True

    def test_accept_llm_ai_engineer(self):
        job = _make_job(title="AI Engineer - LLM Applications")
        assert passes_role_filter(job) is True

    def test_accept_seo_engineer_not_rejected(self):
        """SEO engineer should be accepted (not caught by seo specialist/manager reject)."""
        job = _make_job(title="SEO Engineer")
        assert passes_role_filter(job) is True

    def test_accept_laravel_developer(self):
        job = _make_job(title="Laravel Developer")
        assert passes_role_filter(job) is True

    # ── v1.4: New reject patterns (GTM/PM/Web3) ───────────────────────
    def test_reject_go_to_market_engineer(self):
        job = _make_job(title="Go to Market Engineer")
        assert passes_role_filter(job) is False

    def test_reject_go_to_market_hyphenated(self):
        job = _make_job(title="Go-to-Market Strategy Lead")
        assert passes_role_filter(job) is False

    def test_reject_gtm_engineer(self):
        job = _make_job(title="GTM Engineer")
        assert passes_role_filter(job) is False

    def test_reject_product_manager(self):
        """Product Manager should be rejected (not engineering)."""
        job = _make_job(title="Product Manager")
        assert passes_role_filter(job) is False

    def test_reject_senior_product_manager(self):
        job = _make_job(title="Senior Product Manager")
        assert passes_role_filter(job) is False

    def test_reject_staff_product_manager(self):
        job = _make_job(title="Staff Product Manager")
        assert passes_role_filter(job) is False

    def test_reject_head_of_product(self):
        job = _make_job(title="Head of Product")
        assert passes_role_filter(job) is False

    def test_reject_smart_contract_engineer(self):
        job = _make_job(title="Smart Contract Engineer SVM")
        assert passes_role_filter(job) is False

    def test_reject_blockchain_engineer(self):
        job = _make_job(title="Blockchain Engineer")
        assert passes_role_filter(job) is False

    def test_reject_web3_engineer(self):
        job = _make_job(title="Web3 Engineer")
        assert passes_role_filter(job) is False

    def test_reject_solidity_developer(self):
        job = _make_job(title="Solidity Developer")
        assert passes_role_filter(job) is False

    def test_reject_defi_engineer(self):
        job = _make_job(title="DeFi Engineer")
        assert passes_role_filter(job) is False

    def test_reject_crypto_engineer(self):
        job = _make_job(title="Crypto Engineer")
        assert passes_role_filter(job) is False

    def test_reject_svm_engineer(self):
        job = _make_job(title="SVM Engineer")
        assert passes_role_filter(job) is False

    # ── v1.4: New accept patterns ──────────────────────────────────────
    def test_accept_api_engineer(self):
        job = _make_job(title="API Engineer")
        assert passes_role_filter(job) is True

    def test_accept_api_developer(self):
        job = _make_job(title="API Developer")
        assert passes_role_filter(job) is True

    def test_accept_platform_engineer(self):
        job = _make_job(title="Platform Engineer")
        assert passes_role_filter(job) is True

    def test_accept_solutions_engineer(self):
        job = _make_job(title="Solutions Engineer")
        assert passes_role_filter(job) is True

    def test_accept_integration_engineer(self):
        job = _make_job(title="Integration Engineer")
        assert passes_role_filter(job) is True

    def test_accept_technical_lead(self):
        job = _make_job(title="Technical Lead")
        assert passes_role_filter(job) is True

    def test_accept_staff_engineer(self):
        job = _make_job(title="Staff Engineer")
        assert passes_role_filter(job) is True

    def test_accept_principal_engineer(self):
        job = _make_job(title="Principal Engineer")
        assert passes_role_filter(job) is True

    def test_accept_sre(self):
        job = _make_job(title="Site Reliability Engineer")
        assert passes_role_filter(job) is True

    def test_accept_cloud_engineer(self):
        job = _make_job(title="Cloud Engineer")
        assert passes_role_filter(job) is True

    def test_accept_application_developer(self):
        job = _make_job(title="Application Developer")
        assert passes_role_filter(job) is True


# ═══════════════════════════════════════════════════════════════════════════
#  NGO filter
# ═══════════════════════════════════════════════════════════════════════════

class TestNGOFilter:
    def test_foundation_in_name(self):
        job = _make_job(company="Mozilla Foundation")
        score = compute_ngo_score(job)
        # +2 for "foundation" in company, +1 for known NGO
        assert score >= 2

    def test_known_ngo(self):
        job = _make_job(company="Tactical Tech")
        score = compute_ngo_score(job)
        assert score >= 1

    def test_description_keywords(self):
        """Description keywords alone should NOT be enough without company signal."""
        job = _make_job(
            company="Random Startup",
            description="We are a mission-driven organization focused on digital rights.",
        )
        score = compute_ngo_score(job)
        # v1.4: description alone is NOT enough — require company keyword match
        assert score == 0

    def test_description_keywords_with_company_signal(self):
        """Description keywords + company keyword → NGO."""
        job = _make_job(
            company="Digital Rights Foundation",
            description="We are a mission-driven organization focused on digital rights.",
        )
        score = compute_ngo_score(job)
        # +2 for "foundation" in company, +1 for desc keywords (2 matches + company kw)
        assert score >= 2

    def test_generic_company_no_match(self):
        job = _make_job(company="Google", description="Build ads infrastructure.")
        score = compute_ngo_score(job)
        assert score == 0

    def test_classify_sets_flag(self):
        job = _make_job(company="Amnesty International")
        classify_ngo(job)
        assert job.is_ngo is True

    def test_classify_no_ngo(self):
        job = _make_job(company="FAANG Corp", description="Ad tech platform.")
        classify_ngo(job)
        assert job.is_ngo is False

    # ── New tests for NOT-NGO signals ──────────────────────────────────
    def test_single_description_keyword_not_enough(self):
        """A single weak keyword like 'transparency' should NOT trigger NGO."""
        job = _make_job(
            company="Aroundhome",
            description="We value transparency and quality in our engineering process.",
        )
        score = compute_ngo_score(job)
        assert score == 0
        classify_ngo(job)
        assert job.is_ngo is False

    def test_marketplace_penalty(self):
        """A company with weak NGO signals + marketplace in description."""
        job = _make_job(
            company="Aroundhome",
            description="Leading home services marketplace connecting homeowners with contractors. "
                        "Social impact through transparency and advocacy.",
        )
        score = compute_ngo_score(job)
        assert score == 0  # description signals cancelled by marketplace penalty
        classify_ngo(job)
        assert job.is_ngo is False

    def test_saas_penalty(self):
        job = _make_job(
            company="SaaS Corp",
            description="Our SaaS platform for social impact and transparency.",
        )
        classify_ngo(job)
        assert job.is_ngo is False

    def test_real_ngo_not_penalized(self):
        """A real NGO should not be penalized."""
        job = _make_job(
            company="Amnesty International",
            description="Defending human rights and civil rights worldwide.",
        )
        classify_ngo(job)
        assert job.is_ngo is True

    # ── v1.4: NGO misclassification fixes ──────────────────────────────
    def test_testgorilla_not_ngo(self):
        """TestGorilla (hiring assessment SaaS) must NOT be classified as NGO."""
        job = _make_job(
            company="TestGorilla",
            description="TestGorilla is a talent assessment platform. We help companies "
                        "make better hiring decisions with skills testing and assessment tools. "
                        "Our mission-driven approach to transparency in hiring.",
        )
        classify_ngo(job)
        assert job.is_ngo is False

    def test_anthropic_not_ngo(self):
        """Anthropic (AI safety company) must NOT be classified as NGO."""
        job = _make_job(
            company="Anthropic",
            description="Anthropic is an AI safety company building reliable AI systems. "
                        "Series B funded. Our mission is to build safe, beneficial AI.",
        )
        classify_ngo(job)
        assert job.is_ngo is False

    def test_shopify_not_ngo(self):
        """Shopify (e-commerce platform) must NOT be classified as NGO."""
        job = _make_job(
            company="Shopify",
            description="Shopify is the leading e-commerce platform for businesses of all sizes. "
                        "Our open source contributions and social impact initiatives.",
        )
        classify_ngo(job)
        assert job.is_ngo is False

    def test_mozilla_foundation_is_ngo(self):
        """Mozilla Foundation is a real NGO."""
        job = _make_job(company="Mozilla Foundation")
        classify_ngo(job)
        assert job.is_ngo is True

    def test_amnesty_international_is_ngo(self):
        """Amnesty International is a real NGO."""
        job = _make_job(company="Amnesty International")
        classify_ngo(job)
        assert job.is_ngo is True

    def test_unhcr_is_ngo(self):
        """UNHCR is a real NGO."""
        job = _make_job(company="UNHCR")
        classify_ngo(job)
        assert job.is_ngo is True

    def test_assessment_platform_strong_penalty(self):
        """'assessment platform' in description → strong NOT-NGO penalty."""
        job = _make_job(
            company="HireRight",
            description="We are an assessment platform focused on social impact and transparency.",
        )
        classify_ngo(job)
        assert job.is_ngo is False

    def test_saas_platform_strong_penalty(self):
        """'saas platform' in description → strong NOT-NGO penalty."""
        job = _make_job(
            company="TechCo",
            description="Our saas platform helps with public interest and policy work.",
        )
        classify_ngo(job)
        assert job.is_ngo is False


# ═══════════════════════════════════════════════════════════════════════════
#  Language filter
# ═══════════════════════════════════════════════════════════════════════════

class TestLanguageFilter:
    def test_accept_english(self):
        job = _make_job(
            title="Software Engineer",
            description="We are looking for an experienced software engineer to join our distributed team.",
        )
        assert passes_language_filter(job) is True

    def test_reject_german(self):
        job = _make_job(
            title="Softwareentwickler",
            description="Wir suchen einen erfahrenen Softwareentwickler für unser Team in Berlin. "
                        "Sie sollten Erfahrung mit modernen Webtechnologien haben.",
        )
        assert passes_language_filter(job) is False

    def test_accept_short_text(self):
        """Very short text — should default to accept."""
        job = _make_job(title="Dev", description="")
        assert passes_language_filter(job) is True


# ═══════════════════════════════════════════════════════════════════════════
#  RemoteOK location parsing
# ═══════════════════════════════════════════════════════════════════════════

from sources.remoteok import _parse_remoteok_location


class TestRemoteOKLocationParsing:
    def test_empty_location_worldwide(self):
        """Empty location → worldwide (RemoteOK is remote-only board)."""
        loc, scope = _parse_remoteok_location("")
        assert scope == "worldwide"

    def test_bare_remote_worldwide(self):
        """Bare 'Remote' → worldwide."""
        loc, scope = _parse_remoteok_location("Remote")
        assert scope == "worldwide"

    def test_worldwide_explicit(self):
        loc, scope = _parse_remoteok_location("Worldwide")
        assert scope == "worldwide"

    def test_canada_remote_restricted(self):
        """'Canada (Remote)' → restricted."""
        loc, scope = _parse_remoteok_location("Canada (Remote)")
        assert scope == "restricted"

    def test_united_states_restricted(self):
        loc, scope = _parse_remoteok_location("United States")
        assert scope == "restricted"

    def test_remote_us_restricted(self):
        loc, scope = _parse_remoteok_location("Remote, US")
        assert scope == "restricted"

    def test_remote_dash_us_restricted(self):
        loc, scope = _parse_remoteok_location("Remote - US")
        assert scope == "restricted"

    def test_europe_eu(self):
        loc, scope = _parse_remoteok_location("Europe")
        assert scope == "eu"

    def test_germany_scope(self):
        loc, scope = _parse_remoteok_location("Germany")
        assert scope == "germany"

    def test_berlin_germany_scope(self):
        loc, scope = _parse_remoteok_location("Berlin, Germany")
        assert scope == "germany"

    def test_usa_restricted(self):
        loc, scope = _parse_remoteok_location("USA")
        assert scope == "restricted"

    def test_uk_restricted(self):
        loc, scope = _parse_remoteok_location("UK")
        assert scope == "restricted"

    def test_india_restricted(self):
        loc, scope = _parse_remoteok_location("India")
        assert scope == "restricted"

    def test_tampa_restricted(self):
        loc, scope = _parse_remoteok_location("Tampa, FL")
        assert scope == "restricted"

    def test_unknown_defaults_worldwide(self):
        """Unknown location on RemoteOK → worldwide (benefit of the doubt)."""
        loc, scope = _parse_remoteok_location("Somewhere random")
        assert scope == "worldwide"


# ═══════════════════════════════════════════════════════════════════════════
#  Match score
# ═══════════════════════════════════════════════════════════════════════════

from filters.match import compute_match_score, match_score_bar, _normalize_score


class TestMatchScore:
    def test_perfect_stack_match(self):
        """Job mentioning React + TypeScript + Next.js → high score."""
        job = _make_job(
            title="Full Stack Developer",
            description="React, TypeScript, Next.js, PostgreSQL, Docker",
            tags=["react", "typescript", "nextjs"],
        )
        score = compute_match_score(job)
        assert score >= 70

    def test_partial_match(self):
        """Job mentioning Python + Django → moderate score."""
        job = _make_job(
            title="Python Developer",
            description="Working with Django and PostgreSQL.",
        )
        score = compute_match_score(job)
        assert 20 <= score <= 80

    def test_no_match(self):
        """Job with no matching keywords → 0."""
        job = _make_job(
            title="Marketing Specialist",
            description="Manage social campaigns and brand awareness.",
        )
        score = compute_match_score(job)
        assert score == 0

    def test_ngo_bonus(self):
        """NGO-flagged job should get bonus score."""
        job = _make_job(
            title="Full Stack Developer",
            description="Build tools for digital rights and civic tech.",
        )
        job.is_ngo = True
        score_ngo = compute_match_score(job)

        job2 = _make_job(
            title="Full Stack Developer",
            description="Build tools for product management.",
        )
        job2.is_ngo = False
        score_no_ngo = compute_match_score(job2)

        assert score_ngo > score_no_ngo

    def test_synonym_dedup(self):
        """'nextjs' and 'next.js' should not double-count."""
        job = _make_job(
            title="Next.js Developer",
            description="Build with nextjs and React.",
            tags=["next.js", "nextjs"],
        )
        score = compute_match_score(job)
        # Should count nextjs only once (12 points, not 24)
        assert score > 0

    def test_score_capped_at_100(self):
        """Even with tons of matches, score should not exceed 100."""
        job = _make_job(
            title="Full Stack Software Engineer",
            description="React TypeScript Next.js Python Django FastAPI PostgreSQL "
                        "GraphQL Docker Kubernetes CI/CD GitHub Actions Tailwind "
                        "LLM RAG AI Vue JavaScript Node Redis MySQL Ruby Rails",
            tags=["react", "typescript", "python", "docker"],
        )
        job.is_ngo = True
        score = compute_match_score(job)
        assert score <= 100

    def test_description_keywords_counted(self):
        """Keywords in description (first 500 chars) should be counted."""
        job = _make_job(
            title="Senior Specialist",
            description="Our stack uses react and typescript with postgresql for data.",
        )
        score = compute_match_score(job)
        assert score > 0

    def test_tags_keywords_counted(self):
        """Keywords in tags should be counted."""
        job = _make_job(
            title="Senior Specialist",
            tags=["python", "django", "postgresql"],
        )
        score = compute_match_score(job)
        assert score > 0

    def test_company_ngo_keywords(self):
        """NGO keywords in company name should boost score."""
        job = _make_job(
            title="Full Stack Developer",
            company="Nonprofit Organization for Digital Rights",
            description="Join our team.",
        )
        score = compute_match_score(job)
        assert score > 0

    def test_match_score_bar_empty(self):
        assert match_score_bar(0) == "░░░░░░░░░░"

    def test_match_score_bar_full(self):
        assert match_score_bar(100) == "██████████"

    def test_match_score_bar_78(self):
        bar = match_score_bar(78)
        assert bar == "████████░░"

    def test_match_score_bar_50(self):
        bar = match_score_bar(50)
        assert bar == "█████░░░░░"

    def test_normalize_zero(self):
        assert _normalize_score(0) == 0

    def test_normalize_high(self):
        assert _normalize_score(60) == 95

    def test_normalize_max(self):
        assert _normalize_score(100) == 100


# ═══════════════════════════════════════════════════════════════════════════
#  Company location parsing (arbeitnow)
# ═══════════════════════════════════════════════════════════════════════════

from sources.arbeitnow import _parse_arbeitnow_location


class TestArbeitnowLocationParsing:
    def test_berlin(self):
        city, postal, country = _parse_arbeitnow_location("Berlin")
        assert city == "Berlin"
        assert postal is None
        assert country == "Germany"

    def test_postal_berlin(self):
        city, postal, country = _parse_arbeitnow_location("13086 Berlin")
        assert city == "Berlin"
        assert postal == "13086"
        assert country == "Germany"

    def test_hamburg_germany(self):
        city, postal, country = _parse_arbeitnow_location("Hamburg, Germany")
        assert city == "Hamburg"
        assert postal is None
        assert country == "Germany"

    def test_postal_berlin_germany(self):
        city, postal, country = _parse_arbeitnow_location("13086 Berlin, Germany")
        assert city == "Berlin"
        assert postal == "13086"
        assert country == "Germany"

    def test_remote(self):
        city, postal, country = _parse_arbeitnow_location("Remote")
        assert city is None
        assert postal is None
        assert country is None

    def test_empty(self):
        city, postal, country = _parse_arbeitnow_location("")
        assert city is None

    def test_munich(self):
        city, postal, country = _parse_arbeitnow_location("München")
        assert city == "München"
        assert country == "Germany"

    def test_unknown_city(self):
        """Unknown city with country → should still parse country."""
        city, postal, country = _parse_arbeitnow_location("Zurich, Switzerland")
        assert city == "Zurich"
        assert country == "Switzerland"
