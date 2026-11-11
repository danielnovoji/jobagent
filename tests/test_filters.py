import pytest
from jobagent.filters import (
    passes_clearance,
    passes_description_seniority,
    passes_location,
    passes_title_seniority,
    should_fetch,
)


# --- location ---

@pytest.mark.parametrize("loc", [
    "Israel, Tel Aviv",
    "Tel Aviv",
    "Haifa, Israel",
    "Rehovot, Israel",
    "Migdal HaEmek, Israel",
    "Remote",
    "Worldwide",
    "Global",
    "Hybrid - Israel",
    "",          # unknown → keep
    "Yavne, Israel",
    "Israel, Kiryat-Gat",
    "תל אביב - יפו, Israel",   # how Jobify jobs are stamped
    "ישראל",
    "Remote - EMEA",
    # Forms LinkedIn alert emails use.
    "Tel Aviv-Yafo (Remote)",
    "Kfar Saba (Hybrid)",
    "Raanana (Hybrid)",
    "Center District, Israel (On-site)",
    "Beer Yaakov (On-site)",
    "Kadima - Zoran (Hybrid)",
    "Yehud Monosson (On-site)",
    "Virtual",
])
def test_passes_location_keep(loc):
    assert passes_location(loc) is True


@pytest.mark.parametrize("loc", [
    "United States, Santa Clara",
    "Munich, Germany",
    "Austin, Texas",
    "London, UK",
    "Tokyo, Japan",
    # Remote from somewhere else: these all got through before.
    "Virtual/Remote USA",
    "JPN-Japan-Remote-KLA",
    "CHN-Zhejiang-Ningbo-Remote-KLA",
    "Milpitas, CA; Virtual/Remote USA",
    "Hybrid - Munich",
    # ", il" used to match these as Israel.
    "Chicago, IL",
    "Springfield, Illinois",
    # Unresolved Workday placeholder.
    "2 Locations",
    "Lodz, Poland",
])
def test_passes_location_drop(loc):
    assert passes_location(loc) is False


# --- title seniority ---

@pytest.mark.parametrize("title", [
    "Software Engineer",
    "C++ Developer",
    "Embedded Linux Engineer",
    "Systems Developer",
    "Associate Software Engineer",
    "Junior Backend Developer",
    "Algorithm Engineer",
])
def test_passes_title_seniority_keep(title):
    assert passes_title_seniority(title) is True


@pytest.mark.parametrize("title", [
    "Senior Software Engineer",
    "Sr. C++ Developer",
    "Staff Engineer",
    "Principal Engineer",
    "Lead Software Engineer",
    "Tech Lead",
    "Engineering Manager",
    "Director of Engineering",
    "VP of Engineering",
    "Head of Backend",
    "Chief Architect",
    # Non-SW role types
    "Lab Technician",
    "Process Technician",
    "Servicetechniker/in im Außendienst",
    "Physicist",
    "Research Physicist",
    "Chemist",
    "Supply Chain Analyst",
    "Electrician",
    "Buyer",
    "Experienced Buyer",
    "Talent Acquisition Coordinator",
    "Financial Analyst",
])
def test_passes_title_seniority_drop(title):
    assert passes_title_seniority(title) is False


@pytest.mark.parametrize("title", ["מפתח/ת בכיר/ה", "מפתחת בכירה", "ראש צוות פיתוח", "מנהל מוצר"])
def test_passes_title_seniority_drops_hebrew(title):
    assert passes_title_seniority(title) is False


@pytest.mark.parametrize("title", ["מפתח/ת Fullstack", "מפתח/ת תוכנה", "מהנדס/ת מערכת"])
def test_passes_title_seniority_keeps_hebrew(title):
    assert passes_title_seniority(title) is True


# Daniel graduated and isn't enrolled: student-only roles are out.
@pytest.mark.parametrize("title", [
    "AI SW Engineering Student",
    "Student Intern - C++",
    "Software Engineering Students",
    "סטודנט/ית למדעי המחשב",
    "משרת סטודנט - פיתוח תוכנה",
    "סטודנטית לפיתוח",
])
def test_passes_title_seniority_drops_student_roles(title):
    assert passes_title_seniority(title) is False


@pytest.mark.parametrize("title", ["New Grad Software Engineer", "Graduate Software Engineer"])
def test_passes_title_seniority_keeps_graduate_roles(title):
    assert passes_title_seniority(title) is True


# --- description seniority ---

def test_description_seniority_drops_5_plus():
    assert passes_description_seniority("Requires 5+ years of C++ experience") is False
    assert passes_description_seniority("Minimum 7 years experience required") is False
    assert passes_description_seniority("10+ years in systems programming") is False


def test_description_seniority_keeps_3_plus():
    assert passes_description_seniority("3+ years experience preferred") is True
    assert passes_description_seniority("2-4 years of C++ experience") is True


@pytest.mark.parametrize("text", [
    "At least 5 years of experience as a mechanical engineer",
    "related work experience of 5 years",
    "5 or more years in embedded systems",
    "5-12 years of experience as an Electrical Engineer",
    "ניסיון של 5-7 שנים בפיתוח Fullstack",
    "ניסיון: 5-6 שנים",
    "דרישות: 6 שנות ניסיון",
    "5+ שנות ניסיון ב-C++",
])
def test_description_seniority_drops_real_requirements(text):
    assert passes_description_seniority(text) is False


@pytest.mark.parametrize("text", [
    # Company history: these sentences used to drop good jobs.
    "accelerated computing for more than 25 years. It's a unique legacy",
    "With over 40 years of semiconductor process control experience",
    "For over 15 years, our company has led the market.",
    # Ranges starting below 5 are a stretch, not a drop (CLAUDE.md).
    "3-6 years of experience working with processor architecture",
    "2-5 years of experience in Quality Engineering",
    "3 – 5 years of related experience",
    "ניסיון של 2-3 שנים",
    "פרויקט של 3 חודשים",
])
def test_description_seniority_keeps_non_requirements(text):
    assert passes_description_seniority(text) is True


def test_description_seniority_keeps_empty():
    assert passes_description_seniority("") is True


# --- should_fetch ---

def test_should_fetch_passes_both():
    assert should_fetch("Software Engineer", "Tel Aviv, Israel") is True


def test_should_fetch_fails_location():
    assert should_fetch("Software Engineer", "Austin, Texas") is False


def test_should_fetch_fails_title():
    assert should_fetch("Senior Software Engineer", "Tel Aviv, Israel") is False


def test_should_fetch_empty_location():
    assert should_fetch("Software Engineer", "") is True


# --- clearance filter ---

@pytest.mark.parametrize("desc", [
    "Security clearance required for this role",
    "Must have Secret clearance",
    "Top Secret/SCI clearance required",
    "TS/SCI clearance required",
    "Candidates must be security-cleared",
    "This position requires clearance required",
    "Access to classified information",
])
def test_passes_clearance_drops(desc):
    assert passes_clearance(desc) is False


@pytest.mark.parametrize("desc", [
    "Build secure distributed systems",
    "Experience with security best practices",
    "Work on network security features",
    "C++ Linux multithreading",
    "",
])
def test_passes_clearance_keeps(desc):
    assert passes_clearance(desc) is True


@pytest.mark.parametrize("loc", ["Petah-Tikva", "Beer-Sheva", "Rehovot,ISR", "Mig Ha'emek,ISR",
                                 "IL, Haifa", "Tel Aviv-Yafo, Tel Aviv District, Israel"])
def test_passes_location_spellings_seen_on_real_boards(loc):
    assert passes_location(loc) is True


@pytest.mark.parametrize("loc", ["Chicago, IL", "Isernia, Italy"])
def test_passes_location_isr_and_il_stay_strict(loc):
    assert passes_location(loc) is False


# Non-engineering roles are dropped before they cost a description fetch and a model call.
@pytest.mark.parametrize("title", ["Strategic Account Executive - Public Sector", "Sales Engineer",
                                   "Business Development Representative", "Product Marketing Lead",
                                   "Legal Counsel", "Bookkeeper - Temporary Position", "UX Designer",
                                   "Customer Success Engineer", "נציג/ת מכירות", "רכז/ת גיוס"])
def test_passes_title_seniority_drops_non_engineering(title):
    assert passes_title_seniority(title) is False


@pytest.mark.parametrize("title", ["Design Verification Engineer - Camera", "Salesforce Developer",
                                   "Embedded Software Engineer", "Software Engineer, Tools & Infrastructure",
                                   "Legacy Systems Engineer"])
def test_passes_title_seniority_keeps_engineering(title):
    assert passes_title_seniority(title) is True


@pytest.mark.parametrize("loc", ["Haifa, Israel", "Netanya", "Rehovot,ISR"])
def test_passes_clearance_keeps_israeli_jobs(loc):
    """An Israeli clearance (סיווג ביטחוני) is obtainable for Daniel."""
    assert passes_clearance("Security clearance required (סיווג ביטחוני)", loc) is True


def test_passes_clearance_still_drops_foreign_clearance():
    assert passes_clearance("Active TS/SCI clearance required", "Remote") is False
