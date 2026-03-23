"""Tests for v1.5 Filter Quality Overhaul.

Covers:
  - Expanded role reject patterns (20+ new patterns)
  - Stack compatibility filter (new filters/stack.py)
  - Recalibrated match score weights
  - 80,000 Hours pre-filter for non-dev roles
  - Stepstone remote detection
  - On-site Germany rejection
  - Minimum match score filter
  - End-to-end filter pipeline integration
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.job import Job


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
#  PROBLEM 1: Expanded role reject patterns
# ═══════════════════════════════════════════════════════════════════════════

from filters.role import passes_role_filter


class TestRoleFilterV15RejectPatterns:
    """Test new reject patterns added in v1.5."""

    # ── Wrong tech stack ───────────────────────────────────────────────

    def test_reject_cpp_developer(self):
        job = _make_job(title="C++ Developer")
        assert passes_role_filter(job) is False

    def test_reject_cpp_engineer(self):
        job = _make_job(title="C++ Engineer")
        assert passes_role_filter(job) is False

    def test_reject_cpp_software(self):
        job = _make_job(title="C++ Software Developer for Speech Recognition")
        assert passes_role_filter(job) is False

    def test_reject_java_developer(self):
        job = _make_job(title="Java Developer")
        assert passes_role_filter(job) is False

    def test_reject_java_engineer(self):
        job = _make_job(title="Java Engineer")
        assert passes_role_filter(job) is False

    def test_reject_java_backend(self):
        job = _make_job(title="Senior Java Backend Engineer (Core Java, Trading Systems)")
        assert passes_role_filter(job) is False

    def test_reject_spring_boot(self):
        job = _make_job(title="Spring Boot Developer")
        assert passes_role_filter(job) is False

    def test_reject_java_spring(self):
        job = _make_job(title="Java Spring Microservices Engineer")
        assert passes_role_filter(job) is False

    def test_reject_csharp_developer(self):
        job = _make_job(title="C# Developer")
        assert passes_role_filter(job) is False

    def test_reject_dotnet_developer(self):
        job = _make_job(title=".NET Developer")
        assert passes_role_filter(job) is False

    def test_reject_golang_developer(self):
        job = _make_job(title="Golang Developer")
        assert passes_role_filter(job) is False

    def test_reject_go_developer(self):
        job = _make_job(title="Go Developer")
        assert passes_role_filter(job) is False

    def test_reject_rust_developer(self):
        job = _make_job(title="Rust Developer")
        assert passes_role_filter(job) is False

    def test_reject_rust_engineer(self):
        job = _make_job(title="Rust Engineer")
        assert passes_role_filter(job) is False

    def test_reject_embedded_developer(self):
        job = _make_job(title="Embedded Developer")
        assert passes_role_filter(job) is False

    def test_reject_embedded_engineer(self):
        job = _make_job(title="Embedded Engineer")
        assert passes_role_filter(job) is False

    def test_reject_firmware_engineer(self):
        job = _make_job(title="Firmware Engineer")
        assert passes_role_filter(job) is False

    # ── Infrastructure / DevOps (pure roles) ───────────────────────────

    def test_reject_devops_engineer(self):
        job = _make_job(title="DevOps Engineer CI/CD (m/w/d)")
        assert passes_role_filter(job) is False

    def test_reject_sre(self):
        job = _make_job(title="Site Reliability Engineer")
        assert passes_role_filter(job) is False

    def test_reject_platform_engineer(self):
        job = _make_job(title="Platform Engineer")
        assert passes_role_filter(job) is False

    def test_reject_infrastructure_engineer(self):
        job = _make_job(title="Infrastructure Engineer")
        assert passes_role_filter(job) is False

    def test_reject_systems_administrator(self):
        job = _make_job(title="Systems Administrator")
        assert passes_role_filter(job) is False

    def test_reject_sysadmin(self):
        job = _make_job(title="Linux Sysadmin")
        assert passes_role_filter(job) is False

    def test_reject_linux_administrator(self):
        job = _make_job(title="Linux System Admin / DevOps Engineer (m/w/d)")
        assert passes_role_filter(job) is False

    def test_reject_network_engineer(self):
        job = _make_job(title="Network Engineer")
        assert passes_role_filter(job) is False

    def test_reject_cloud_architect(self):
        job = _make_job(title="Cloud Architect")
        assert passes_role_filter(job) is False

    def test_reject_kubernetes_engineer(self):
        job = _make_job(title="Kubernetes Engineering Consultant (w/m/d)")
        assert passes_role_filter(job) is False

    def test_reject_kubernetes_consultant(self):
        job = _make_job(title="Kubernetes Consultant")
        assert passes_role_filter(job) is False

    def test_reject_solutions_architect(self):
        job = _make_job(title="IT Solution Architect - Kubernetes (w/m/d)")
        assert passes_role_filter(job) is False

    # ── Research and science ───────────────────────────────────────────

    def test_reject_research_engineer(self):
        job = _make_job(title="Research Engineer")
        assert passes_role_filter(job) is False

    def test_reject_ml_engineer(self):
        job = _make_job(title="ML Engineer - Embodied AI")
        assert passes_role_filter(job) is False

    def test_reject_ai_researcher(self):
        job = _make_job(title="AI Researcher")
        assert passes_role_filter(job) is False

    def test_reject_machine_learning_researcher(self):
        job = _make_job(title="Machine Learning Researcher")
        assert passes_role_filter(job) is False

    def test_reject_data_scientist(self):
        job = _make_job(title="Data Scientist")
        assert passes_role_filter(job) is False

    def test_reject_data_engineer(self):
        job = _make_job(title="Data Engineer, Safeguards")
        assert passes_role_filter(job) is False

    def test_reject_quantitative_analyst(self):
        job = _make_job(title="Quantitative Analyst")
        assert passes_role_filter(job) is False

    def test_reject_mathematical_modeller(self):
        job = _make_job(title="Mathematical Modeller, Vaccine-Preventable Diseases")
        assert passes_role_filter(job) is False

    def test_reject_research_laboratory(self):
        job = _make_job(title="Research Laboratory Technician, Centre for Climate Repair")
        assert passes_role_filter(job) is False

    def test_reject_benchside(self):
        job = _make_job(title="Benchside Software Engineer, Wet Lab")
        assert passes_role_filter(job) is False

    # ── Security ───────────────────────────────────────────────────────

    def test_reject_security_engineer(self):
        job = _make_job(title="Security Engineer")
        assert passes_role_filter(job) is False

    def test_reject_penetration_tester(self):
        job = _make_job(title="Penetration Tester")
        assert passes_role_filter(job) is False

    def test_reject_security_researcher(self):
        job = _make_job(title="Security Researcher")
        assert passes_role_filter(job) is False

    def test_reject_identity_engineer(self):
        job = _make_job(title="Senior Identity Engineer")
        assert passes_role_filter(job) is False

    def test_reject_offensive_security(self):
        job = _make_job(title="Principal Offensive Security Developer")
        assert passes_role_filter(job) is False

    # ── QA / Testing ──────────────────────────────────────────────────

    def test_reject_qa_engineer(self):
        job = _make_job(title="QA Engineer")
        assert passes_role_filter(job) is False

    def test_reject_quality_engineer(self):
        job = _make_job(title="Quality Engineer")
        assert passes_role_filter(job) is False

    def test_reject_test_engineer(self):
        job = _make_job(title="Senior Professional Test Engineer (w/m/d)")
        assert passes_role_filter(job) is False

    def test_reject_test_specialist(self):
        job = _make_job(title="Test Specialist, Quality Engineering (2026)")
        assert passes_role_filter(job) is False

    def test_reject_quality_assurance(self):
        job = _make_job(title="Quality Assurance Lead")
        assert passes_role_filter(job) is False

    # ── Product / Design ──────────────────────────────────────────────

    def test_reject_product_owner(self):
        job = _make_job(title="Product Owner (m/w/d) Softwareentwicklung")
        assert passes_role_filter(job) is False

    def test_reject_product_operations(self):
        job = _make_job(title="Product Operations Lead")
        assert passes_role_filter(job) is False

    def test_reject_product_capability(self):
        job = _make_job(title="Sr. Product Capability Architect - REMOTE")
        assert passes_role_filter(job) is False

    def test_reject_3d_artist(self):
        job = _make_job(title="Lead 3D Environment Artist")
        assert passes_role_filter(job) is False

    def test_reject_motion_designer(self):
        job = _make_job(title="Motion Designer")
        assert passes_role_filter(job) is False

    # ── Other non-dev ─────────────────────────────────────────────────

    def test_reject_linguist(self):
        job = _make_job(title="Simplified Chinese Marketing and Product Remote Linguist")
        assert passes_role_filter(job) is False

    def test_reject_translator(self):
        job = _make_job(title="Technical Translator")
        assert passes_role_filter(job) is False

    def test_reject_request_for_proposals(self):
        job = _make_job(title="Request for Proposals, AI Interpretability (2026)")
        assert passes_role_filter(job) is False

    def test_reject_rfp(self):
        job = _make_job(title="RFP, Red Team, Lie Detection Competition")
        assert passes_role_filter(job) is False

    def test_reject_content_strategist(self):
        job = _make_job(title="Content Strategist")
        assert passes_role_filter(job) is False

    def test_reject_growth_manager(self):
        job = _make_job(title="Growth Manager")
        assert passes_role_filter(job) is False

    def test_reject_talent_team_lead(self):
        job = _make_job(title="Talent Team Lead, Product, Design, & Engineering")
        assert passes_role_filter(job) is False

    # ── v1.5: Still accepted (important edge cases) ───────────────────

    def test_accept_platform_developer(self):
        """'Platform Developer' is accepted (different from 'Platform Engineer')."""
        job = _make_job(title="Platform Developer")
        assert passes_role_filter(job) is True

    def test_accept_internal_tools_engineer(self):
        """'Internal Tools Engineer' is accepted."""
        job = _make_job(title="Internal Tools Engineer")
        assert passes_role_filter(job) is True

    def test_accept_technical_lead(self):
        job = _make_job(title="Technical Lead")
        assert passes_role_filter(job) is True

    def test_accept_senior_fullstack_react_django(self):
        job = _make_job(title="Senior Full-Stack (Python/Angular) Engineer")
        assert passes_role_filter(job) is True

    def test_accept_software_engineer(self):
        job = _make_job(title="Software Engineer")
        assert passes_role_filter(job) is True

    def test_accept_react_developer(self):
        job = _make_job(title="React Developer")
        assert passes_role_filter(job) is True

    def test_accept_frontend_developer(self):
        job = _make_job(title="Frontend Developer")
        assert passes_role_filter(job) is True

    def test_accept_backend_developer(self):
        job = _make_job(title="Backend Developer")
        assert passes_role_filter(job) is True

    def test_accept_web_developer(self):
        job = _make_job(title="Web Developer")
        assert passes_role_filter(job) is True

    def test_accept_python_developer(self):
        """'Data Engineering' in a broader Python Developer title is NOT 'data engineer'."""
        job = _make_job(title="Python Developer (m/w/d) - Backend & Data Engineering")
        assert passes_role_filter(job) is True

    def test_accept_django_developer(self):
        job = _make_job(title="Django Developer")
        assert passes_role_filter(job) is True

    def test_accept_fullstack_developer(self):
        job = _make_job(title="Full Stack Developer (RoR/React/React Native)")
        assert passes_role_filter(job) is True

    def test_accept_ai_engineer_llm(self):
        """AI Engineer with LLM focus is product dev."""
        job = _make_job(title="AI Engineer - LLM Applications")
        assert passes_role_filter(job) is True

    def test_accept_wordpress_support_engineer(self):
        """WordPress Support Engineer is still a dev role (has 'engineer')."""
        job = _make_job(title="WordPress Support Engineer")
        assert passes_role_filter(job) is True

    def test_accept_senior_backend_ai_cloud(self):
        job = _make_job(title="Senior Backend / Full-Stack Developer - AI & Cloud")
        assert passes_role_filter(job) is True


# ═══════════════════════════════════════════════════════════════════════════
#  PROBLEM 2: Stack compatibility filter
# ═══════════════════════════════════════════════════════════════════════════

from filters.stack import passes_stack_filter


class TestStackFilter:
    """Test the new stack compatibility filter."""

    # ── Pure incompatible stack → REJECT ──────────────────────────────

    def test_reject_pure_java_spring(self):
        """Java Spring Boot with no user stack signals → reject."""
        job = _make_job(
            title="Senior Software Engineer Java Spring Boot",
            tags=["Java", "Spring Boot", "Microservices"],
        )
        assert passes_stack_filter(job) is False

    def test_reject_pure_csharp_dotnet(self):
        job = _make_job(
            title="C# .NET Developer",
            tags=["C#", ".NET", "Azure"],
        )
        assert passes_stack_filter(job) is False

    def test_reject_pure_kotlin(self):
        job = _make_job(
            title="Kotlin Developer",
            tags=["Kotlin", "Android"],
        )
        assert passes_stack_filter(job) is False

    def test_reject_pure_rust(self):
        job = _make_job(
            title="Rust Engineer",
            tags=["Rust", "Systems"],
        )
        assert passes_stack_filter(job) is False

    def test_reject_navision(self):
        """Microsoft ERP role → reject."""
        job = _make_job(
            title="SOFTWARE DEVELOPER AL / C/AL / MS Navision BC 365",
            tags=[],
        )
        assert passes_stack_filter(job) is False

    def test_reject_salesforce(self):
        job = _make_job(
            title="Salesforce Developer",
            tags=["Salesforce", "Apex"],
        )
        assert passes_stack_filter(job) is False

    def test_reject_unity_developer(self):
        job = _make_job(
            title="Unity Developer",
            tags=["Unity", "C#", "Game Dev"],
        )
        assert passes_stack_filter(job) is False

    def test_reject_ios_swift(self):
        job = _make_job(
            title="iOS Developer",
            tags=["Swift", "iOS"],
        )
        assert passes_stack_filter(job) is False

    def test_reject_sap_developer(self):
        job = _make_job(
            title="SAP Developer",
            tags=["SAP", "ABAP"],
        )
        assert passes_stack_filter(job) is False

    # ── Mixed stacks → ACCEPT ─────────────────────────────────────────

    def test_accept_java_plus_react(self):
        """Java job that also mentions React → ambiguous, keep."""
        job = _make_job(
            title="Full Stack Developer Java React",
            tags=["Java", "React", "TypeScript"],
        )
        assert passes_stack_filter(job) is True

    def test_accept_dotnet_plus_vue(self):
        job = _make_job(
            title=".NET Developer with Vue.js",
            tags=[".NET", "Vue"],
        )
        assert passes_stack_filter(job) is True

    def test_accept_spring_plus_frontend(self):
        job = _make_job(
            title="Full Stack Spring Boot + Angular/React",
            tags=["Spring", "React", "Frontend"],
        )
        assert passes_stack_filter(job) is True

    # ── No incompatible signals → ACCEPT ──────────────────────────────

    def test_accept_generic_software_engineer(self):
        """No incompatible stack → accept (generic title)."""
        job = _make_job(title="Software Engineer", tags=[])
        assert passes_stack_filter(job) is True

    def test_accept_react_typescript(self):
        job = _make_job(
            title="Senior React Developer",
            tags=["React", "TypeScript", "Next.js"],
        )
        assert passes_stack_filter(job) is True

    def test_accept_python_django(self):
        job = _make_job(
            title="Python Developer",
            tags=["Python", "Django", "FastAPI"],
        )
        assert passes_stack_filter(job) is True

    def test_accept_ruby_rails(self):
        job = _make_job(
            title="Rails Developer",
            tags=["Ruby", "Rails"],
        )
        assert passes_stack_filter(job) is True

    def test_accept_fullstack_generic(self):
        job = _make_job(
            title="Full Stack Developer",
            tags=[],
        )
        assert passes_stack_filter(job) is True

    def test_accept_php_symfony(self):
        job = _make_job(
            title="PHP Symfony Backend Developer",
            tags=["PHP", "Symfony"],
        )
        assert passes_stack_filter(job) is True


# ═══════════════════════════════════════════════════════════════════════════
#  PROBLEM 3: 80,000 Hours pre-filter
# ═══════════════════════════════════════════════════════════════════════════


class TestHours80kPreFilter:
    """Test the pre-filter in hours80k.py."""

    def setup_method(self):
        from sources.hours80k import Hours80kSource
        self.source = Hours80kSource()

    # ── Reject non-dev roles ──────────────────────────────────────────

    def test_reject_request_for_proposals(self):
        job = _make_job(title="Request for Proposals, AI Interpretability (2026)")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_rfp(self):
        job = _make_job(title="RFP, Red Team, Lie Detection Competition")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_research_laboratory(self):
        job = _make_job(title="Research Laboratory Technician, Centre for Climate Repair")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_lab_technician(self):
        job = _make_job(title="Lab Technician, Molecular Biology")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_benchside(self):
        job = _make_job(title="Benchside Software Engineer, Wet Lab")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_mathematical_modeller(self):
        job = _make_job(title="Mathematical Modeller, Vaccine-Preventable Diseases")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_quantitative(self):
        job = _make_job(title="Quantitative Analyst")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_wet_lab(self):
        job = _make_job(title="Wet Lab Research Associate")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_policy_analyst(self):
        job = _make_job(title="Policy Analyst")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_programme_officer(self):
        job = _make_job(title="Programme Officer")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_operations_lead(self):
        job = _make_job(title="Operations Lead")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_communications(self):
        job = _make_job(title="Communications Manager")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_fellowship(self):
        job = _make_job(title="AI Safety Fellowship")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_internship(self):
        job = _make_job(title="Research Internship")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_clinical(self):
        job = _make_job(title="Clinical Research Coordinator")
        assert self.source._is_relevant_for_user(job) is False

    # ── Accept dev roles ──────────────────────────────────────────────

    def test_accept_software_engineer(self):
        job = _make_job(title="Software Engineer, Alignment")
        assert self.source._is_relevant_for_user(job) is True

    def test_accept_senior_software_engineer(self):
        job = _make_job(title="Senior Software Engineer")
        assert self.source._is_relevant_for_user(job) is True

    def test_accept_full_stack_developer(self):
        job = _make_job(title="Full Stack Developer")
        assert self.source._is_relevant_for_user(job) is True

    def test_accept_frontend_engineer(self):
        job = _make_job(title="Frontend Engineer")
        assert self.source._is_relevant_for_user(job) is True

    def test_accept_backend_developer(self):
        job = _make_job(title="Backend Developer")
        assert self.source._is_relevant_for_user(job) is True

    def test_accept_devops_engineer(self):
        """DevOps Engineer has 'engineer' — passes 80k pre-filter."""
        job = _make_job(title="DevOps Engineer")
        assert self.source._is_relevant_for_user(job) is True

    def test_accept_platform_engineer(self):
        job = _make_job(title="Platform Engineer")
        assert self.source._is_relevant_for_user(job) is True

    def test_accept_data_engineer(self):
        """Data Engineer kept at the 80k pre-filter level (borderline)."""
        job = _make_job(title="Data Engineer, Safeguards")
        assert self.source._is_relevant_for_user(job) is True

    def test_accept_security_labs_engineer(self):
        """Has 'engineer' in title → passes pre-filter."""
        job = _make_job(title="Security Labs Engineer")
        assert self.source._is_relevant_for_user(job) is True

    # ── Specific jobs from today's scan ───────────────────────────────

    def test_reject_ai_safety_accelerator(self):
        """'AI Safety Research Accelerator' — no dev signal → reject."""
        job = _make_job(title="AI Safety Research Accelerator")
        assert self.source._is_relevant_for_user(job) is False

    def test_reject_aicraft_program(self):
        """'AICRAFT Program' — no dev signal → reject."""
        job = _make_job(title="AICRAFT Program")
        assert self.source._is_relevant_for_user(job) is False


# ═══════════════════════════════════════════════════════════════════════════
#  PROBLEM 4: Recalibrated match score
# ═══════════════════════════════════════════════════════════════════════════

from filters.match import compute_match_score, _normalize_score


class TestMatchScoreV15:
    """Test recalibrated match score weights."""

    def test_react_django_high_score(self):
        """Senior Full Stack React Django → high match (>70%)."""
        job = _make_job(
            title="Senior Full Stack React Django Developer",
            description="React, Django, TypeScript, PostgreSQL, Docker",
            tags=["react", "django", "typescript"],
        )
        score = compute_match_score(job)
        assert score >= 70, f"Expected >= 70, got {score}"

    def test_java_only_low_score(self):
        """Pure Java job → low score (<20%) due to negative weights."""
        job = _make_job(
            title="Java Developer",
            description="Spring Boot, Microservices, Java 17",
            tags=["java", "spring boot"],
        )
        score = compute_match_score(job)
        assert score < 20, f"Expected < 20, got {score}"

    def test_ngo_react_very_high_score(self):
        """React + NGO → very high score."""
        job = _make_job(
            title="Full Stack Developer",
            company="Tactical Tech",
            description="React, TypeScript, Next.js, Tailwind CSS",
            tags=["react", "typescript", "nextjs"],
        )
        job.is_ngo = True
        score = compute_match_score(job)
        assert score >= 85, f"Expected >= 85, got {score}"

    def test_python_fastapi_moderate_score(self):
        """Python + FastAPI → solid moderate score."""
        job = _make_job(
            title="Python Developer",
            description="Build APIs with FastAPI and PostgreSQL",
            tags=["python", "fastapi"],
        )
        score = compute_match_score(job)
        assert score >= 50, f"Expected >= 50, got {score}"

    def test_vue_nuxt_high_score(self):
        """Vue + Nuxt → high score (part of core stack)."""
        job = _make_job(
            title="Vue.js Developer",
            description="Nuxt.js, TypeScript, Tailwind CSS",
            tags=["vue", "nuxt", "typescript"],
        )
        score = compute_match_score(job)
        assert score >= 65, f"Expected >= 65, got {score}"

    def test_langchain_rag_bonus(self):
        """LangChain + RAG → good score (AI product building)."""
        job = _make_job(
            title="AI Engineer",
            description="Build RAG pipelines using LangChain and LLM APIs",
            tags=["langchain", "rag", "python"],
        )
        score = compute_match_score(job)
        assert score >= 60, f"Expected >= 60, got {score}"

    def test_negative_weights_reduce_score(self):
        """C++ in description should reduce score via negative weight."""
        # Job with react (positive) and C++ (negative)
        job_mixed = _make_job(
            title="Software Engineer",
            description="React frontend with C++ backend components",
            tags=["react", "c++"],
        )
        score_mixed = compute_match_score(job_mixed)

        # Job with just react (no negative)
        job_clean = _make_job(
            title="Software Engineer",
            description="React frontend with Node.js backend",
            tags=["react", "node"],
        )
        score_clean = compute_match_score(job_clean)

        assert score_clean > score_mixed, (
            f"Clean score ({score_clean}) should be > mixed score ({score_mixed})"
        )

    def test_tactical_tech_high_bonus(self):
        """'tactical tech' in company → +20 bonus."""
        job = _make_job(
            title="Software Developer",
            company="Tactical Tech",
        )
        score = compute_match_score(job)
        assert score >= 30, f"Expected >= 30 with tactical tech bonus, got {score}"

    def test_generic_title_no_stack_zero(self):
        """Generic title with no stack keywords → 0."""
        job = _make_job(
            title="Manager",
            description="Lead a team of professionals.",
        )
        score = compute_match_score(job)
        assert score == 0


# ═══════════════════════════════════════════════════════════════════════════
#  PROBLEM 5: Stepstone remote detection
# ═══════════════════════════════════════════════════════════════════════════


class TestStepstoneRemoteDetection:
    """Test Stepstone remote/hybrid signal detection."""

    def setup_method(self):
        from sources.stepstone import StepstoneSource
        self.source = StepstoneSource()

    def test_remote_in_title(self):
        """Title containing 'Remote' → is_remote=True."""
        posting = {
            "titel": "Python Developer (Remote)",
            "refnr": "ref1",
            "externeUrl": "https://example.com/1",
            "arbeitgeber": "TestCo",
            "arbeitsort": {},
        }
        job = self.source._parse_posting(posting)
        assert job.is_remote is True

    def test_homeoffice_in_title(self):
        """Title containing 'Homeoffice' → is_remote=True."""
        posting = {
            "titel": "Fullstack Developer Homeoffice",
            "refnr": "ref2",
            "externeUrl": "https://example.com/2",
            "arbeitgeber": "TestCo",
            "arbeitsort": {},
        }
        job = self.source._parse_posting(posting)
        assert job.is_remote is True

    def test_hybrid_in_title(self):
        """Title containing 'Hybrid' → is_remote=True."""
        posting = {
            "titel": "Senior Developer (Hybrid)",
            "refnr": "ref3",
            "externeUrl": "https://example.com/3",
            "arbeitgeber": "TestCo",
            "arbeitsort": {},
        }
        job = self.source._parse_posting(posting)
        assert job.is_remote is True

    def test_remote_scope_always_germany(self):
        """Stepstone jobs always have scope=germany."""
        posting = {
            "titel": "Software Engineer",
            "refnr": "ref4",
            "externeUrl": "https://example.com/4",
            "arbeitgeber": "TestCo",
            "arbeitsort": {},
        }
        job = self.source._parse_posting(posting)
        assert job.remote_scope == "germany"

    def test_default_still_remote_from_api_filter(self):
        """Even without explicit signal, API filter means likely remote."""
        posting = {
            "titel": "Software Developer",
            "refnr": "ref5",
            "externeUrl": "https://example.com/5",
            "arbeitgeber": "TestCo",
            "arbeitsort": {},
        }
        job = self.source._parse_posting(posting)
        # The API filters for "ho" (homeoffice) so default is True
        assert job.is_remote is True


# ═══════════════════════════════════════════════════════════════════════════
#  PROBLEM 5b: On-site Germany rejection
# ═══════════════════════════════════════════════════════════════════════════


class TestOnsiteGermanyRejection:
    """Test ACCEPT_ONSITE_GERMANY config in filter pipeline."""

    def test_reject_onsite_germany_when_disabled(self):
        """Germany-scope, is_remote=False → rejected when ACCEPT_ONSITE_GERMANY=False."""
        from main import _apply_filters
        with patch("main.config") as mock_config:
            mock_config.COMPANY_BLOCKLIST = []
            mock_config.FILTER_SENIOR_ONLY = False
            mock_config.MIN_SALARY_EUR = 0
            mock_config.MAX_JOB_AGE_DAYS = 14
            mock_config.SOURCE_MAX_AGE_DAYS = {}
            mock_config.MINIMUM_MATCH_SCORE = 0
            mock_config.ACCEPT_ONSITE_GERMANY = False

            job = _make_job(
                title="React Developer",
                location="Berlin, Germany",
                url="https://example.com/onsite1",
                is_remote=False,
            )
            job.remote_scope = "germany"
            results = _apply_filters([job])
            assert len(results) == 0

    def test_accept_onsite_germany_when_enabled(self):
        """Germany-scope, is_remote=False → accepted when ACCEPT_ONSITE_GERMANY=True."""
        from main import _apply_filters
        with patch("main.config") as mock_config:
            mock_config.COMPANY_BLOCKLIST = []
            mock_config.FILTER_SENIOR_ONLY = False
            mock_config.MIN_SALARY_EUR = 0
            mock_config.MAX_JOB_AGE_DAYS = 14
            mock_config.SOURCE_MAX_AGE_DAYS = {}
            mock_config.MINIMUM_MATCH_SCORE = 0
            mock_config.ACCEPT_ONSITE_GERMANY = True

            job = _make_job(
                title="React Developer",
                location="Berlin, Germany (Remote)",
                url="https://example.com/onsite2",
                is_remote=False,
            )
            job.remote_scope = "germany"
            results = _apply_filters([job])
            assert len(results) == 1

    def test_accept_remote_germany_regardless(self):
        """Germany-scope, is_remote=True → accepted regardless of config."""
        from main import _apply_filters
        with patch("main.config") as mock_config:
            mock_config.COMPANY_BLOCKLIST = []
            mock_config.FILTER_SENIOR_ONLY = False
            mock_config.MIN_SALARY_EUR = 0
            mock_config.MAX_JOB_AGE_DAYS = 14
            mock_config.SOURCE_MAX_AGE_DAYS = {}
            mock_config.MINIMUM_MATCH_SCORE = 0
            mock_config.ACCEPT_ONSITE_GERMANY = False

            job = _make_job(
                title="React Developer",
                location="Berlin, Germany (Remote)",
                url="https://example.com/remote1",
                is_remote=True,
            )
            job.remote_scope = "germany"
            results = _apply_filters([job])
            assert len(results) == 1


# ═══════════════════════════════════════════════════════════════════════════
#  PROBLEM 6: Minimum match score filter
# ═══════════════════════════════════════════════════════════════════════════


class TestMinimumMatchScore:
    """Test MINIMUM_MATCH_SCORE config in filter pipeline."""

    def test_minimum_score_zero_accepts_all(self):
        """Default MINIMUM_MATCH_SCORE=0 → no filtering on score."""
        from main import _apply_filters
        with patch("main.config") as mock_config:
            mock_config.COMPANY_BLOCKLIST = []
            mock_config.FILTER_SENIOR_ONLY = False
            mock_config.MIN_SALARY_EUR = 0
            mock_config.MAX_JOB_AGE_DAYS = 14
            mock_config.SOURCE_MAX_AGE_DAYS = {}
            mock_config.MINIMUM_MATCH_SCORE = 0
            mock_config.ACCEPT_ONSITE_GERMANY = False

            job = _make_job(
                title="Software Developer",
                location="Remote - Worldwide",
                url="https://example.com/score1",
            )
            results = _apply_filters([job])
            assert len(results) == 1

    def test_minimum_score_rejects_low_match(self):
        """MINIMUM_MATCH_SCORE=15 → rejects jobs below 15%."""
        from main import _apply_filters
        with patch("main.config") as mock_config:
            mock_config.COMPANY_BLOCKLIST = []
            mock_config.FILTER_SENIOR_ONLY = False
            mock_config.MIN_SALARY_EUR = 0
            mock_config.MAX_JOB_AGE_DAYS = 14
            mock_config.SOURCE_MAX_AGE_DAYS = {}
            mock_config.MINIMUM_MATCH_SCORE = 15
            mock_config.ACCEPT_ONSITE_GERMANY = False

            # This job has no matching stack keywords → score will be low
            job = _make_job(
                title="Software Developer",
                location="Remote - Worldwide",
                url="https://example.com/score2",
                description="Build enterprise solutions.",
            )
            results = _apply_filters([job])
            # Score for generic "Software Developer" with no keywords ≈ 0
            assert len(results) == 0

    def test_minimum_score_accepts_high_match(self):
        """MINIMUM_MATCH_SCORE=15 → accepts jobs above 15%."""
        from main import _apply_filters
        with patch("main.config") as mock_config:
            mock_config.COMPANY_BLOCKLIST = []
            mock_config.FILTER_SENIOR_ONLY = False
            mock_config.MIN_SALARY_EUR = 0
            mock_config.MAX_JOB_AGE_DAYS = 14
            mock_config.SOURCE_MAX_AGE_DAYS = {}
            mock_config.MINIMUM_MATCH_SCORE = 15
            mock_config.ACCEPT_ONSITE_GERMANY = False

            job = _make_job(
                title="React Developer",
                location="Remote - Worldwide",
                url="https://example.com/score3",
                description="React TypeScript Next.js Django PostgreSQL",
                tags=["react", "typescript"],
            )
            results = _apply_filters([job])
            assert len(results) == 1
            assert results[0].match_score >= 15


# ═══════════════════════════════════════════════════════════════════════════
#  End-to-end filter pipeline: real jobs from today's scan
# ═══════════════════════════════════════════════════════════════════════════


class TestFilterPipelineRealJobs:
    """Test filter pipeline with actual job titles from the March 23 scan."""

    # ── Jobs that SHOULD be rejected ──────────────────────────────────

    def test_reject_principal_ml_engineer(self):
        """Principal ML Engineer → rejected by role filter."""
        job = _make_job(
            title="Principal ML Engineer - Embodied AI Scaling Foundations",
            location="Remote - Worldwide",
        )
        assert passes_role_filter(job) is False

    def test_reject_principal_offensive_security(self):
        job = _make_job(
            title="Principal Offensive Security Developer",
            location="Remote - Worldwide",
        )
        assert passes_role_filter(job) is False

    def test_reject_electrodynamics_engineer(self):
        """Electrodynamics Engineer — passes role filter ('engineer' keyword)
        but would get very low match score."""
        job = _make_job(title="Electrodynamics Engineer - Remote")
        # Has "engineer" → passes role filter (too generic to reject)
        assert passes_role_filter(job) is True
        # But match score will be near 0 (no stack match)
        score = compute_match_score(job)
        assert score < 20

    def test_reject_cad_engineering(self):
        """CAD & Engineering Design Experts — passes role filter ('engineer' substring)
        but gets very low match score."""
        job = _make_job(title="CAD & Engineering Design Experts")
        # Has "engineer" substring → passes role filter
        assert passes_role_filter(job) is True
        score = compute_match_score(job)
        assert score == 0

    def test_reject_research_engineer_societal(self):
        job = _make_job(title="Research Engineer, Societal Impacts")
        assert passes_role_filter(job) is False

    def test_reject_researcher_loss_of_control(self):
        """'Researcher' — no dev keyword match."""
        job = _make_job(title="Researcher, Loss of Control")
        assert passes_role_filter(job) is False

    def test_reject_svp_data_product(self):
        """SVP, Data Product & Insights — C-suite/management."""
        job = _make_job(title="SVP, Data Product & Insights - Life Sciences")
        # "vp of" doesn't match but this doesn't have dev keywords either
        # Actually "product" and "data" are not in ROLE_KEYWORDS
        assert passes_role_filter(job) is False

    def test_reject_product_designer_growth(self):
        """Product Designer → rejected."""
        job = _make_job(title="Product Designer, Growth")
        assert passes_role_filter(job) is False

    # ── Stack filter rejections ───────────────────────────────────────

    def test_reject_senior_java_backend_by_stack(self):
        """Senior Java Backend Engineer → rejected by stack filter."""
        job = _make_job(
            title="Senior Java Backend Engineer (Core Java, Trading Systems)",
            tags=["Java", "Core Java"],
        )
        assert passes_stack_filter(job) is False

    def test_reject_senior_fullstack_java_ee_by_stack(self):
        """Senior Fullstack Developer Java EE → 'java' in title."""
        job = _make_job(
            title="Senior Fullstack Developer - Java EE/Spring/Angular/React",
            tags=["Java", "Spring", "Angular", "React"],
        )
        # Has both java AND react → mixed → ACCEPT
        assert passes_stack_filter(job) is True

    def test_reject_csharp_net_backend(self):
        """Senior Backend Developer C#.NET → rejected by stack."""
        job = _make_job(
            title="Senior Backend Developer - C#.NET (m/w/d)",
            tags=["C#", ".NET"],
        )
        assert passes_stack_filter(job) is False

    def test_reject_software_developer_navision(self):
        """MS Navision → rejected by stack."""
        job = _make_job(
            title="SOFTWARE DEVELOPER AL / C/AL / MS Navision BC 365",
            tags=[],
        )
        assert passes_stack_filter(job) is False

    # ── Jobs that SHOULD be accepted ──────────────────────────────────

    def test_accept_frontend_developer_react(self):
        job = _make_job(
            title="Frontend Developer",
            location="Remote - Germany",
            tags=["react", "typescript"],
        )
        assert passes_role_filter(job) is True
        assert passes_stack_filter(job) is True

    def test_accept_senior_full_stack_react(self):
        job = _make_job(
            title="Senior Full Stack Web Developer – React (m/w/d)",
            location="Germany (Remote)",
        )
        assert passes_role_filter(job) is True
        assert passes_stack_filter(job) is True

    def test_accept_python_developer_backend(self):
        job = _make_job(
            title="Python Developer (m/w/d) - Backend & Data Engineering",
            location="Germany (Remote)",
            tags=["python"],
        )
        assert passes_role_filter(job) is True
        assert passes_stack_filter(job) is True

    def test_accept_lead_developer_vue_nuxt(self):
        job = _make_job(
            title="Lead Developer / Fullstack-Entwickler (m/w/d) — Vue/Nuxt/TS",
            location="Germany (Remote)",
        )
        assert passes_role_filter(job) is True
        assert passes_stack_filter(job) is True

    def test_accept_full_stack_engineer(self):
        job = _make_job(
            title="Full Stack Engineer",
            location="Remote - EU",
        )
        assert passes_role_filter(job) is True
        assert passes_stack_filter(job) is True

    def test_accept_senior_software_engineer(self):
        job = _make_job(
            title="Senior Software Engineer",
            location="Remote - Worldwide",
        )
        assert passes_role_filter(job) is True
        assert passes_stack_filter(job) is True

    def test_accept_backend_developer(self):
        job = _make_job(
            title="Backend Developer",
            location="Remote - EU",
        )
        assert passes_role_filter(job) is True
        assert passes_stack_filter(job) is True

    def test_accept_junior_backend_engineer(self):
        """Junior is only rejected by senior filter (off by default)."""
        job = _make_job(
            title="Junior Backend Engineer",
            location="Remote - EU",
        )
        assert passes_role_filter(job) is True

    def test_accept_software_engineer_ios_core_product(self):
        """'Software Engineer, iOS Core Product' — has 'software engineer'."""
        job = _make_job(
            title="Software Engineer, iOS Core Product",
            location="Remote - EU",
        )
        assert passes_role_filter(job) is True


# ═══════════════════════════════════════════════════════════════════════════
#  Config: new settings
# ═══════════════════════════════════════════════════════════════════════════


class TestConfigV15:
    """Test that new config vars exist and have correct types."""

    def test_minimum_match_score_exists(self):
        import config
        assert hasattr(config, "MINIMUM_MATCH_SCORE")
        assert isinstance(config.MINIMUM_MATCH_SCORE, int)

    def test_accept_onsite_germany_exists(self):
        import config
        assert hasattr(config, "ACCEPT_ONSITE_GERMANY")
        assert isinstance(config.ACCEPT_ONSITE_GERMANY, bool)


# ═══════════════════════════════════════════════════════════════════════════
#  Score normalization tiers
# ═══════════════════════════════════════════════════════════════════════════


class TestScoreNormalizationTiers:
    """Verify the new tiered normalization curve."""

    def test_tier_boundaries(self):
        """Check each tier boundary value."""
        assert _normalize_score(0) == 0
        assert _normalize_score(5) == 20
        assert _normalize_score(15) == 50
        assert _normalize_score(25) == 70
        assert _normalize_score(40) == 90
        assert _normalize_score(50) == 95

    def test_within_tier_1(self):
        """5 < raw < 15 → 20..50."""
        score = _normalize_score(10)
        assert 20 <= score <= 50

    def test_within_tier_2(self):
        """15 < raw < 25 → 50..70."""
        score = _normalize_score(20)
        assert 50 <= score <= 70

    def test_within_tier_3(self):
        """25 < raw < 40 → 70..90."""
        score = _normalize_score(32)
        assert 70 <= score <= 90

    def test_within_tier_4(self):
        """40 < raw < 50 → 90..95."""
        score = _normalize_score(45)
        assert 90 <= score <= 95

    def test_negative_raw(self):
        """Negative raw → 0."""
        assert _normalize_score(-10) == 0

    def test_very_high_raw(self):
        """raw=100 → capped at 100."""
        assert _normalize_score(100) == 100
