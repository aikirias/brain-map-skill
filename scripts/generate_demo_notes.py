#!/usr/bin/env python3
"""
Generate a ~1000-note fictional knowledge base in save-note format.

Three themes: Work (Aurora Dynamics, robotics co), Study (Vesalius University
neurobiology MSc), Life (immigration / routines / relationship-psychology).

Output = a folder of Markdown with YAML frontmatter, two-section bodies
(## Summary / ## Original), [[wikilinks]], People cards, Meetings, Journal,
TODO lists — exactly what the save-note skill writes, but in bulk. The same
tree feeds an Obsidian vault AND a gbrain import.

All people, orgs, events are invented. No real data.
"""
import os, random, datetime, re, textwrap, sys

random.seed(20260612)

VAULT = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Projects/brain-map-demo/vault")

START = datetime.date(2024, 6, 1)
END = datetime.date(2026, 6, 12)
SPAN = (END - START).days

def rdate(lo=0.0, hi=1.0):
    d = START + datetime.timedelta(days=int(random.uniform(lo, hi) * SPAN))
    h, m = random.randint(7, 22), random.randint(0, 59)
    return datetime.datetime(d.year, d.month, d.day, h, m)

def ts(dt):  # full ISO
    return dt.strftime("%Y-%m-%dT%H:%M:%S")

def tsmin(dt):
    return dt.strftime("%Y-%m-%d %H:%M")

def slugify(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:80]

WRITTEN = {}  # path -> None, for collision avoidance
def safe_name(base):
    name = base
    i = 2
    while name in WRITTEN:
        name = f"{base} ({i})"; i += 1
    WRITTEN[name] = None
    return name

def write(folder, title, frontmatter, body):
    os.makedirs(os.path.join(VAULT, folder), exist_ok=True)
    fname = safe_name(re.sub(r'[\\/:*?"<>|]', "-", title))
    path = os.path.join(VAULT, folder, fname + ".md")
    fm = ["---"]
    for k, v in frontmatter.items():
        if isinstance(v, list):
            fm.append(f"{k}: [{', '.join(v)}]")
        else:
            fm.append(f"{k}: {v}")
    fm.append("---\n")
    with open(path, "w") as f:
        f.write("\n".join(fm) + body.rstrip() + "\n")
    return title

def note_body(summary, original):
    return f"# {title_cache}\n\n## Summary\n\n{summary}\n\n## Original\n\n> {original}\n"

# we set title_cache per write for the H1
title_cache = ""
def page(folder, title, tags, dt, summary, original, source=None, related=None):
    global title_cache
    title_cache = title
    fm = {"tags": tags, "created": ts(dt)}
    if source: fm["source"] = source
    body = f"# {title}\n\n## Summary\n\n{summary}\n\n## Original\n\n> {original}\n"
    write(folder, title, fm, body)
    return title

def simple_page(folder, title, tags, dt, body_md, source=None):
    fm = {"tags": tags, "created": ts(dt)}
    if source: fm["source"] = source
    write(folder, title, fm, f"# {title}\n\n{body_md}\n")
    return title

def link(name): return f"[[{name}]]"

count = {"work": 0, "study": 0, "life": 0}
def bump(t): count[t] += 1

# ─────────────────────────────────────────────────────────────────────────────
# NAME POOLS
# ─────────────────────────────────────────────────────────────────────────────
FIRST = ["Mara","Theo","Ines","Kofi","Lena","Diego","Priya","Sven","Yuki","Noor",
         "Caleb","Aisha","Marco","Hana","Owen","Zara","Felix","Ravi","Clara","Bram",
         "Sofia","Niko","Tariq","Elin","Joon","Petra","Amir","Mei","Lukas","Dalia",
         "Ola","Reza","Vera","Hugo","Sana","Emil","Nadia","Pablo","Iris","Kenji"]
LAST = ["Okafor","Voss","Mendez","Halloran","Sato","Bauer","Nakamura","Petrov","Costa",
        "Yilmaz","Bergstrom","Adeyemi","Rossi","Larsen","Khan","Novak","Ferraro","Dubois",
        "Haas","Maric","Solberg","Ibrahim","Lindqvist","Park","Romano","Kowalski","Hassan",
        "Engel","Vidal","Schmitt","Andersson","Farouk","Toth","Marchetti","Bjork"]
used_names = set()
def person_name():
    while True:
        n = f"{random.choice(FIRST)} {random.choice(LAST)}"
        if n not in used_names:
            used_names.add(n); return n

# ═════════════════════════════════════════════════════════════════════════════
# THEME 1 — WORK : Aurora Dynamics (robotics + embodied-AI company)
# ═════════════════════════════════════════════════════════════════════════════
W = "Work"
work_people = []  # (name, role, team)

ROLES = [
    ("Iris Solberg","CEO","Exec"),
    ("Marco Rossi","CTO","Exec"),
    ("Petra Novak","VP Engineering","Engineering"),
    ("Tariq Hassan","VP Product","Product"),
    ("Clara Dubois","Head of People","People"),
    ("Sven Bauer","Eng Manager — Perception","Perception"),
    ("Mei Park","Eng Manager — Motion","Motion"),
    ("Owen Halloran","Eng Manager — Platform","Platform"),
    ("Priya Khan","Staff Engineer — Perception","Perception"),
    ("Diego Mendez","Senior Engineer — Perception","Perception"),
    ("Hana Sato","Senior Engineer — Motion","Motion"),
    ("Felix Haas","Engineer — Motion","Motion"),
    ("Noor Ibrahim","Senior Engineer — Platform","Platform"),
    ("Caleb Voss","Engineer — Platform","Platform"),
    ("Zara Adeyemi","Product Manager — Fleet","Product"),
    ("Lukas Engel","Designer","Product"),
    ("Vera Petrov","QA Lead","Quality"),
    ("Amir Farouk","SRE","Platform"),
    ("Elin Bergstrom","Data Scientist","Perception"),
    ("Ravi Nair","Engineer — Perception","Perception"),
]
for nm, role, team in ROLES:
    used_names.add(nm); work_people.append((nm, role, team))
# extra ICs
for _ in range(10):
    nm = person_name(); team = random.choice(["Perception","Motion","Platform","Quality"])
    work_people.append((nm, "Engineer — "+team, team))

VALUES = ["Safety is the feature","Earn trust in small steps","Default to candor",
          "Ship, then sharpen","Customer reality over internal opinion","Leave the codebase kinder"]

mgr_of = {"Perception":"Sven Bauer","Motion":"Mei Park","Platform":"Owen Halloran","Quality":"Vera Petrov"}

# Org chart page
org_lines = ["**Aurora Dynamics** — embodied-AI / warehouse robotics. Fictional company.\n",
             "## Leadership",
             f"- {link('Iris Solberg')} — CEO",
             f"  - {link('Marco Rossi')} — CTO",
             f"    - {link('Petra Novak')} — VP Engineering",
             f"  - {link('Tariq Hassan')} — VP Product",
             f"  - {link('Clara Dubois')} — Head of People",
             "\n## Engineering"]
for team in ["Perception","Motion","Platform","Quality"]:
    org_lines.append(f"### {team} — managed by {link(mgr_of[team])}")
    for nm, role, t in work_people:
        if t == team and nm != mgr_of[team]:
            org_lines.append(f"- {link(nm)} — {role}")
simple_page(W, "Aurora Dynamics — Org Chart", ["work","org","reference"], rdate(0,0.05),
            "\n".join(org_lines)); bump("work")

# Values page
simple_page(W, "Aurora Dynamics — Values", ["work","values","culture"], rdate(0,0.05),
            "Our six operating values:\n\n" + "\n".join(f"{i+1}. **{v}**" for i,v in enumerate(VALUES))
            ); bump("work")

# Tool guides
simple_page(W, "Guide — Jira at Aurora", ["work","guide","jira","tools"], rdate(0,0.1),
    textwrap.dedent("""\
    How we use Jira. Project keys: **PERC** (Perception), **MOT** (Motion),
    **PLAT** (Platform), **QA** (Quality).

    - Ticket title = imperative, scoped: `PERC-1421: Debounce depth-camera dropouts`.
    - Every PR references its ticket; merge is blocked without one.
    - Statuses: Backlog → Ready → In Progress → In Review → Verify → Done.
    - Weekly grooming Thursdays; estimate in points (Fibonacci, cap at 8 — split bigger).
    - Incidents get an `INC-` linked ticket + a postmortem page in Confluence.
    See """ + link("Guide — Git & Code Review") + " and " + link("Guide — Confluence") + ".")); bump("work")

simple_page(W, "Guide — Git & Code Review", ["work","guide","git","tools","ethics"], rdate(0,0.1),
    textwrap.dedent("""\
    Branch = `<jira-id>-<slug>`. Commit subject `PERC-1421: Title` (≤50 chars), body explains *why*.

    **Review etiquette (the human part):**
    - Review the code, not the coder. No "you", prefer "this function".
    - Block only on correctness, security, or data-loss. Style → suggest, don't gate.
    - Approve with a nit ≠ block. Trust the author to take the nit.
    - First review within one working day; if you can't, say so.
    Reflects our value *""" + VALUES[5] + "*. See " + link("Guide — Jira at Aurora") + ".")); bump("work")

simple_page(W, "Guide — Confluence", ["work","guide","confluence","tools"], rdate(0,0.1),
    "Design docs, postmortems, team handbooks live in Confluence. One-pager template for "
    "every project ≥1 week. Decisions recorded as ADRs. Link the Jira epic at the top."); bump("work")

simple_page(W, "Guide — On-call & Incidents", ["work","guide","oncall","sre"], rdate(0,0.12),
    "Weekly rotation per team. Page = Sev1/Sev2 only. Ack in 5 min. "
    f"Incident commander coordinates, scribe logs timeline. {link('Amir Farouk')} owns the runbook. "
    "Blameless postmortem within 48h."); bump("work")

simple_page(W, "Team Working Agreement — Engineering", ["work","ethics","culture","team"], rdate(0,0.12),
    "How we treat each other:\n\n- Core hours 10:00–16:00; async otherwise.\n"
    "- Disagree-and-commit after a decision is logged.\n- No silent PRs older than 2 days.\n"
    "- Psychological safety: it's fine to say 'I don't know'.\n- Friday demo, no slides required.\n"
    f"Owned by {link('Petra Novak')}; reflects *{VALUES[1]}* and *{VALUES[2]}*."); bump("work")

# Onboarding
simple_page(W, "Onboarding — First Week Checklist", ["work","onboarding","guide"], rdate(0,0.15),
    "1. SSO + 2FA.\n2. Clone the monorepo, run `make bootstrap`.\n3. Read "
    + link("Aurora Dynamics — Values") + ", " + link("Team Working Agreement — Engineering") +
    ".\n4. Pick a 'good first issue' in Jira.\n5. 1:1 with your manager + buddy."); bump("work")

# People cards (work)
for nm, role, team in work_people:
    mgr = mgr_of.get(team, "Petra Novak") if "Manager" not in role and "VP" not in role and role!="CEO" else ""
    who = f"{role}, **{team}** team at Aurora Dynamics."
    if mgr and nm != mgr: who += f" Reports to {link(mgr)}."
    body = f"{who}\n\n## Mentions\n"
    simple_page(os.path.join(W,"People"), nm, ["person","work",slugify(team)], rdate(0,0.2), body)
    bump("work")

# Meetings (work) — standups, 1:1s, design reviews, retros
WORK_MEETINGS = []
mtypes = [("Design Review", "design"), ("1:1", "one-on-one"), ("Sprint Retro","retro"),
          ("Incident Review","incident"), ("Roadmap Sync","planning"), ("Architecture Review","design")]
topics = ["depth-camera calibration drift","grasp-planning latency","fleet telemetry schema",
          "motion-planner replan storms","sim-to-real gap on pallets","battery thermal cutoffs",
          "perception model rollback","gripper firmware OTA","warehouse map versioning",
          "PLAT API rate limits","QA flaky-test quarantine","data-labeling pipeline backlog"]
for i in range(82):
    mt, mtag = random.choice(mtypes)
    dt = rdate(0.1, 1.0)
    topic = random.choice(topics)
    atts = random.sample([p[0] for p in work_people], k=random.randint(2,5))
    title = f"{dt.date()} {mt} — {topic}"
    decisions = random.choice([
        "Agreed to ship behind a flag and A/B for a week.",
        "Reverted the change; root cause is upstream in PLAT.",
        "Owner assigned; revisit next sprint.",
        "Accepted the risk; documented in the ADR.",
        "Split into two tickets; QA to add a regression test."])
    fm = {"tags":["meeting","work",mtag],"date":str(dt.date()),
          "start":dt.strftime("%H:%M"),"end":"","attendees":['"'+a+'"' for a in atts],
          "location":random.choice(["Zoom","Room Helix","Room Cortex","Async thread"]),
          "gcal_event_id":'""',"created":ts(dt)}
    body = (f"# {title}\n\n**Attendees:** " + ", ".join(link(a) for a in atts) +
            f"\n\n## Agenda\n- {topic}\n\n## Notes\n- Discussed {topic}; tradeoffs weighed.\n"
            f"\n## Decision\n- {decisions}\n\n## Action items\n- [ ] Follow up on {topic} "
            f"_(added {tsmin(dt)})_")
    write(os.path.join(W,"Meetings"), title, fm, body)
    WORK_MEETINGS.append(title); bump("work")

# Work situation notes
SIT = [
 ("Postmortem — depth sensor brownout","Sev2: 40-minute partial fleet stall after a firmware OTA browned out depth sensors. Rollback fixed it. Action: stage OTA to 5% first.","perception","incident"),
 ("Decision — adopt RRT* for replanning","Motion team picks RRT* over lattice planner for tight-aisle replans; 18% fewer stalls in sim.","motion","decision"),
 ("Win — labeling backlog cleared","Cut the labeling backlog from 9k to 0 frames with an active-learning sampler. Model AP +3.1.","perception","win"),
 ("Risk — single point of failure in map service","Map service has no replica; if it dies the whole site halts. Filed PLAT epic.","platform","risk"),
 ("Customer escalation — Pallet site #7","Robots mis-grasp shrink-wrapped pallets at site 7. Hypothesis: specular reflections fool depth.","perception","customer"),
]
for base, summ, tag, kind in SIT:
    for k in range(24):  # variations to reach volume
        dt = rdate(0.1,1.0)
        who = random.choice([p[0] for p in work_people])
        t = f"{base}" if k==0 else f"{base} — update {k}"
        page(W, t, ["work",tag,kind], dt,
             f"{summ}\n\nOwner: {link(who)}. Tracked in Jira. Relates to "
             f"{link(random.choice(WORK_MEETINGS))}.",
             f"{summ} (note {k})"); bump("work")

# Work journal
WJ_LINES = ["Paired with {p} on {t} — finally understood why the planner thrashes.",
            "Rough day: {t} regressed in prod, spent it bisecting.",
            "Demo went well, {p} liked the latency win on {t}.",
            "1:1 with {p}: career chat, want to grow into the {t} space.",
            "Shipped the {t} fix. Quiet pride. {v}.",
            "Frustrated by review latency on {t}; raised it in retro."]
for i in range(98):
    dt = rdate(0.1,1.0)
    p = random.choice([p[0] for p in work_people]); t = random.choice(topics); v = random.choice(VALUES)
    txt = random.choice(WJ_LINES).format(p=p, t=t, v=v)
    page(os.path.join(W,"Journal"), f"Work journal — {dt.date()} #{i}", ["work","journal"], dt,
         txt.replace(p, link(p)), txt); bump("work")

# Work TODO list (one running file)
wtodos = []
for i in range(55):
    dt = rdate(0.5,1.0)
    item = random.choice([
        "review PERC-{} depth filter","write ADR for {}","prep 1:1 notes for {}",
        "add regression test for {}","follow up incident on {}","update runbook section on {}"]).format(
        random.choice(topics) if "{}" in "x" else "")
    item = item.format(random.choice(topics)) if "{}" in item else item
    wtodos.append(f"- [ ] {item} _(added {tsmin(dt)})_")
simple_page(W, "TODO", ["work","todo"], rdate(0.5,1.0), "# Work TODO\n\n" + "\n".join(wtodos))
bump("work")

# Work theme index
simple_page(W, "Work — Index", ["work","index","moc"], rdate(0.9,1.0),
    "Map of content for **Aurora Dynamics**.\n\n- " + link("Aurora Dynamics — Org Chart") +
    "\n- " + link("Aurora Dynamics — Values") + "\n- " + link("Team Working Agreement — Engineering") +
    "\n- " + link("Guide — Jira at Aurora") + "\n- " + link("Guide — Git & Code Review") +
    "\n- " + link("Onboarding — First Week Checklist")); bump("work")

# ═════════════════════════════════════════════════════════════════════════════
# THEME 2 — STUDY : Vesalius University, MSc Neurobiology
# ═════════════════════════════════════════════════════════════════════════════
S = "Study"
SUBJECTS = [
 ("Cellular Neuroscience","NB501"),("Computational Neuroscience","NB512"),
 ("Neuroanatomy","NB504"),("Systems Neuroscience","NB520"),
 ("Synaptic Plasticity","NB531"),("Neuropharmacology","NB540"),
 ("Cognitive Neuroscience","NB550"),("Statistics for Neuroscience","NB560"),
 ("Neural Data Analysis","NB565"),("Developmental Neurobiology","NB572"),
 ("Neuroimaging Methods","NB580"),("Ethics in Neuroscience","NB590"),
]
PROF = []
prof_specialty = {
 "Cellular Neuroscience":"ion channels & patch-clamp",
 "Computational Neuroscience":"spiking network models",
 "Neuroanatomy":"connectomics of the cortex",
 "Systems Neuroscience":"hippocampal place cells",
 "Synaptic Plasticity":"LTP/LTD mechanisms",
 "Neuropharmacology":"GPCR signaling",
 "Cognitive Neuroscience":"decision-making & PFC",
 "Statistics for Neuroscience":"hierarchical Bayesian models",
 "Neural Data Analysis":"spike-sorting & GLMs",
 "Developmental Neurobiology":"axon guidance",
 "Neuroimaging Methods":"7T fMRI",
 "Ethics in Neuroscience":"neuro-data privacy"}
for subj, code in SUBJECTS:
    nm = person_name(); PROF.append((nm, subj, code))

# Professor dossiers
for nm, subj, code in PROF:
    spec = prof_specialty[subj]
    body = (f"Professor of **{subj}** ({code}) at Vesalius University. "
            f"Research focus: {spec}.\n\n"
            f"- Office hours: {random.choice(['Mon','Tue','Wed','Thu'])} "
            f"{random.randint(13,16)}:00, Bldg {random.choice('ABCD')}-{random.randint(100,420)}.\n"
            f"- Grading: {random.choice(['tough but fair','generous on effort','exam-heavy','project-weighted'])}.\n"
            f"- Vibe: {random.choice(['warm, story-driven lectures','dense slides, fast pace','Socratic, cold-calls','chalkboard derivations'])}.\n\n"
            f"## Mentions\n")
    simple_page(os.path.join(S,"People"), nm, ["person","study","professor"], rdate(0,0.2), body)
    bump("study")

# Subject index pages
for subj, code in SUBJECTS:
    prof = next(p[0] for p in PROF if p[1]==subj)
    simple_page(S, f"{code} — {subj}", ["study","subject",slugify(subj)], rdate(0,0.15),
        f"Course **{code} {subj}**, taught by {link(prof)}. Focus: {prof_specialty[subj]}.\n\n"
        f"Assessment: {random.choice(['midterm + final','project + final','weekly quizzes + exam','lab reports + final'])}.\n\n"
        f"Lecture notes tagged `{slugify(subj)}`."); bump("study")

# Schedule pages (per semester)
for sem in ["2024 Fall","2025 Spring","2025 Fall","2026 Spring"]:
    chosen = random.sample(SUBJECTS, 4)
    rows = "\n".join(f"- {random.choice(['Mon','Tue','Wed','Thu','Fri'])} "
                     f"{random.choice(['09:00','11:00','14:00','16:00'])} — {link(c+' — '+s) if False else link(code+' — '+s)}"
                     for s,code in chosen for c in [code])
    simple_page(S, f"Schedule — {sem}", ["study","schedule"], rdate(0,0.5),
        f"Timetable for **{sem}**.\n\n" + "\n".join(
            f"- {random.choice(['Mon','Tue','Wed','Thu','Fri'])} {random.choice(['09:00','11:00','14:00','16:00'])}"
            f" — {link(code+' — '+s)}" for s,code in chosen)); bump("study")

# Lecture notes — subject x weeks
LEC_TOPICS = {
 "Cellular Neuroscience":["resting potential","Hodgkin-Huxley","voltage-gated Na+ channels","Ca2+ dynamics","myelination","patch-clamp basics"],
 "Computational Neuroscience":["LIF neurons","cable theory","rate vs spiking models","STDP rules","attractor networks","balanced E/I"],
 "Neuroanatomy":["cortical layers","thalamocortical loops","basal ganglia","cerebellum circuits","white-matter tracts","brainstem nuclei"],
 "Systems Neuroscience":["place & grid cells","sensory maps","motor cortex coding","reward circuits","sleep & oscillations","attention"],
 "Synaptic Plasticity":["NMDA receptors","LTP induction","LTD","homeostatic scaling","metaplasticity","engram cells"],
 "Neuropharmacology":["dopamine pathways","SSRIs","GABAergic drugs","opioid receptors","psychedelics & 5-HT2A","tolerance"],
 "Cognitive Neuroscience":["working memory","decision-making","value coding","cognitive control","language areas","metacognition"],
 "Statistics for Neuroscience":["GLMs","multiple comparisons","mixed models","bootstrapping","Bayesian priors","power analysis"],
 "Neural Data Analysis":["spike sorting","PSTHs","dimensionality reduction","decoding","cross-validation","GLM encoding"],
 "Developmental Neurobiology":["neural tube","axon guidance cues","critical periods","apoptosis","synaptic pruning","neurogenesis"],
 "Neuroimaging Methods":["BOLD signal","fMRI preprocessing","DTI","MEG vs EEG","7T tradeoffs","decoding fMRI"],
 "Ethics in Neuroscience":["neuro-data privacy","informed consent","animal research 3Rs","enhancement","dual-use","incidental findings"],
}
for subj, code in SUBJECTS:
    prof = next(p[0] for p in PROF if p[1]==subj)
    for wk, topic in enumerate(LEC_TOPICS[subj], 1):
        dt = rdate(0.05, 0.95)
        title = f"{code} W{wk} — {topic}"
        summ = (f"Lecture on **{topic}** ({link(code+' — '+subj)}, {link(prof)}).\n\n"
                f"- Key idea: {topic} underpins {random.choice(['signal propagation','learning','perception','behavior'])}.\n"
                f"- Exam-relevant: {random.choice(['yes, flagged','likely','maybe'])}.\n"
                f"- Follow-up reading assigned.")
        page(os.path.join(S,"Lectures"), title, ["study","lecture",slugify(subj)], dt,
             summ, f"notes on {topic}"); bump("study")

# Course project
proj_advisor = PROF[1][0]  # computational neuro prof
simple_page(S, "Course Project — Decoding Reach Direction from M1", ["study","project","capstone"], rdate(0.2,0.4),
    f"Capstone: decode reach direction from motor-cortex (M1) spike trains using a GLM + "
    f"population vector, then compare to an LSTM. Advisor: {link(proj_advisor)}.\n\n"
    f"Milestones tracked under tag `project`. Dataset: public NHP reaching task."); bump("study")
PROJ_NOTES = ["loaded the dataset, 96-channel array, 8 reach targets",
    "spike-sorting sanity checks; dropped 6 noisy channels",
    "built PSTHs per target — clean tuning curves, nice",
    "population vector decoder hits 71% — baseline set",
    "GLM encoding model; cross-validated log-likelihood up",
    "LSTM overfits with 200 trials; added dropout + early stop",
    "advisor feedback: report confidence intervals, not point estimates",
    "wrote methods section; need a figure of the tuning curves",
    "decoder confusion mostly between adjacent targets — expected",
    "final: GLM 71%, LSTM 74%; modest but honest result"]
for i in range(72):
    dt = rdate(0.25, 0.95)
    base = PROJ_NOTES[i % len(PROJ_NOTES)]
    t = f"Project log — {dt.date()} #{i}"
    page(os.path.join(S,"Project"), t, ["study","project","journal"], dt,
         f"{base}.\n\nRelates to {link('Course Project — Decoding Reach Direction from M1')} "
         f"and {link(proj_advisor)}.", base); bump("study")

# Study journal
SJ = ["Crammed {t} all night; coffee is a nootropic, fight me.",
      "{p}'s lecture on {t} finally made plasticity click.",
      "Bombed the {t} quiz. Regroup.","Study group with classmates on {t} — way better than solo.",
      "Imposter syndrome before the {t} exam, then it went fine.",
      "Office hours with {p}; asked a dumb question, got a kind answer."]
classmates = [person_name() for _ in range(8)]
for nm in classmates:
    simple_page(os.path.join(S,"People"), nm, ["person","study","classmate"], rdate(0,0.3),
        f"Classmate in the MSc Neurobiology cohort.\n\n## Mentions\n"); bump("study")
for i in range(88):
    dt = rdate(0.05,0.95)
    p = random.choice([x[0] for x in PROF]+classmates); subj=random.choice(SUBJECTS)
    txt = random.choice(SJ).format(t=subj[0], p=p)
    page(os.path.join(S,"Journal"), f"Study journal — {dt.date()} #{i}", ["study","journal"], dt,
         txt.replace(p, link(p)), txt); bump("study")

# Study todos
stodos=[]
for i in range(38):
    dt=rdate(0.3,1.0)
    item=random.choice(["read chapter on {}","problem set for {}","email {} about extension",
        "lab report {}","revise flashcards {}","book library slot for {}"])
    if "{}" in item:
        item=item.format(random.choice([s[0] for s in SUBJECTS]))
    stodos.append(f"- [ ] {item} _(added {tsmin(dt)})_")
simple_page(S,"TODO",["study","todo"],rdate(0.3,1.0),"# Study TODO\n\n"+"\n".join(stodos)); bump("study")

simple_page(S,"Study — Index",["study","index","moc"],rdate(0.9,1.0),
    "MSc Neurobiology @ Vesalius University.\n\n## Subjects\n" +
    "\n".join("- "+link(code+" — "+s) for s,code in SUBJECTS) +
    "\n\n## Key pages\n- " + link("Course Project — Decoding Reach Direction from M1")); bump("study")

# ═════════════════════════════════════════════════════════════════════════════
# THEME 3 — LIFE : immigration, routines, relationship-psychology
# ═════════════════════════════════════════════════════════════════════════════
L = "Life"
partner = "Lena Marchetti"; used_names.add(partner)
lawyer = "Hugo Lindqvist"; used_names.add(lawyer)
landlord = "Dalia Toth"; used_names.add(landlord)
therapist = "Sofia Romano"; used_names.add(therapist)
mechanic = "Bram Larsen"; used_names.add(mechanic)
friends = [person_name() for _ in range(8)]

LIFE_PEOPLE = [
 (partner,"partner","My partner. Together ~3 years. Moved abroad together."),
 (lawyer,"immigration","Immigration lawyer handling my work-permit renewal."),
 (landlord,"housing","Landlord for the flat on Birkenweg 12."),
 (therapist,"health","Therapist, weekly sessions. CBT-leaning."),
 (mechanic,"car","Mechanic at Larsen Auto; honest, fair prices."),
]
for nm, tag, desc in LIFE_PEOPLE:
    simple_page(os.path.join(L,"People"), nm, ["person","life",tag], rdate(0,0.2),
        desc + "\n\n## Mentions\n"); bump("life")
for nm in friends:
    simple_page(os.path.join(L,"People"), nm, ["person","life","friend"], rdate(0,0.3),
        "Friend.\n\n## Mentions\n"); bump("life")

# Immigration notes (the spine of life-admin)
simple_page(L,"Immigration — Work Permit Renewal Master Plan",["life","immigration","important"],rdate(0,0.3),
    f"Current permit expires **2026-09-30**. Renewal window opens 90 days prior (from **2026-07-02**).\n\n"
    f"Lawyer: {link(lawyer)}.\n\n## Checklist\n- [ ] Employment letter from {link('Aurora Dynamics — Org Chart')}\n"
    f"- [ ] Last 6 payslips\n- [ ] Proof of address (landlord {link(landlord)})\n"
    f"- [ ] Health insurance certificate\n- [ ] Biometrics appointment\n- [ ] Fee payment receipt\n\n"
    f"**Hard deadline: submit before 2026-08-15** to be safe."); bump("life")

IMMIG = [
 ("Immigration — biometrics appointment booked","Booked biometrics at the migration office. Bring passport + appointment QR.","appointment"),
 ("Immigration — employer letter requested","Asked People team for the employment-confirmation letter. SLA 5 days.","document"),
 ("Immigration — address registration updated","Re-registered address after the move; Meldebescheinigung in hand.","document"),
 ("Immigration — fee paid","Paid the renewal fee online; saved the receipt PDF.","payment"),
 ("Immigration — lawyer call notes","Call with the lawyer: timeline is tight but fine if I file by mid-August.","call"),
 ("Immigration — passport expiry check","Passport valid until 2028 — no renewal needed there, good.","check"),
]
for base, summ, kind in IMMIG:
    for k in range(11):
        dt=rdate(0.2,1.0)
        t = base if k==0 else f"{base} — follow-up {k}"
        page(L,t,["life","immigration",kind],dt,
             f"{summ}\n\nPart of {link('Immigration — Work Permit Renewal Master Plan')}. "
             f"Lawyer {link(lawyer)}.", summ); bump("life")

# Routines: car insurance, fines, taxes, rent, utilities, health
ROUTINES = [
 ("Car insurance renewal","Annual car insurance is due. Compare quotes, switch if >10% cheaper.","car","2026-04-15"),
 ("Pay parking fine","€35 parking fine, ref #install. Pay within 14 days to avoid surcharge.","car","2025-11-20"),
 ("Annual tax filing","File the annual tax return; gather payslips + deductions.","money","2026-05-31"),
 ("Rent — quarterly review","Landlord adjusts rent quarterly; check the new amount.","housing",None),
 ("Health insurance — annual switch window","Open enrolment; decide whether to switch plans.","health","2025-12-15"),
 ("Phone contract renewal","Contract auto-renews; renegotiate or port out.","admin","2026-02-01"),
 ("Car — winter tire swap","Swap to winter tires at the mechanic.","car","2025-10-31"),
 ("Driver's license exchange","Exchange foreign license for a local one before it lapses.","admin","2026-03-30"),
]
life_todos=[]
for name, summ, tag, due in ROUTINES:
    for k in range(12):
        dt=rdate(0.1,1.0)
        t = name if k==0 else f"{name} — {dt.year} cycle {k}"
        extra = f" Due **{due}**." if due else ""
        ref = link(mechanic) if tag=="car" else (link(landlord) if tag=="housing" else "")
        page(L,t,["life","routine",tag],dt, f"{summ}{extra} {ref}".strip(), summ); bump("life")
        if random.random()<0.5:
            life_todos.append(f"- [ ] {name}{(' (due '+due+')') if due else ''} _(added {tsmin(dt)})_")

simple_page(L,"TODO",["life","todo"],rdate(0.3,1.0),"# Life TODO\n\n"+"\n".join(life_todos[:50])); bump("life")

# Relationship / psychology journal (the emotional core)
PSJ = [
 "Argument with {p} about chores; we used the repair phrase and it actually worked.",
 "Therapy with {th}: named the pattern where I withdraw under stress.",
 "Felt homesick today. Called family. {p} cooked the dish from home.",
 "Good week with {p} — long walk, no phones. Connection over content.",
 "Anxiety spike about the permit. {th} taught a grounding exercise. Used it.",
 "Lonely despite the move going well. Made plans with {f}.",
 "Boundaries practice: said no to overtime, protected the evening with {p}.",
 "Gratitude: the small flat feels like home now.",
 "Comparison trap on social media; logged off, journaled instead.",
 "Hard conversation with {p} about the future / where we settle. Honest, scary, good.",
 "Coffee with {f}; talked through the imposter feelings at work.",
 "Slept badly, ruminating. {th} says: schedule the worry, don't carry it.",
]
for i in range(140):
    dt=rdate(0.05,1.0)
    p=partner; th=therapist; f=random.choice(friends)
    txt=random.choice(PSJ).format(p=p,th=th,f=f)
    out=txt.replace(p,link(p)).replace(th,link(th)).replace(f,link(f))
    page(os.path.join(L,"Journal"),f"Life journal — {dt.date()} #{i}",["life","journal","psychology"],dt,
         out, txt); bump("life")

# Life appointments / meetings
for i in range(28):
    dt=rdate(0.1,1.0)
    kind=random.choice([("Doctor visit","health"),("Lawyer meeting","immigration"),
        ("Flat viewing","housing"),("Therapy session","health"),("Bank appointment","money")])
    who = {"health":therapist,"immigration":lawyer,"housing":landlord,"money":person_name()}.get(kind[1],partner)
    title=f"{dt.date()} {kind[0]}"
    fm={"tags":["meeting","life",kind[1]],"date":str(dt.date()),"start":dt.strftime("%H:%M"),
        "end":"","attendees":['"'+who+'"'],"location":random.choice(["Downtown office","Clinic","Online"]),
        "gcal_event_id":'""',"created":ts(dt)}
    write(os.path.join(L,"Meetings"),title,fm,
        f"# {title}\n\n**With:** {link(who)}\n\n## Notes\n- {kind[0]} regarding "
        f"{random.choice(['routine matters','the renewal','a follow-up'])}.\n")
    bump("life")

# Saved links (life)
LINKS=[
 ("How to appeal a parking fine","https://example.org/appeal-fines","Saved a guide on contesting fines; valid grounds + template letter.","car"),
 ("Tax deductions for expats","https://example.org/expat-tax","Checklist of deductions expats often miss.","money"),
 ("Grounding techniques for anxiety","https://example.org/grounding","5-4-3-2-1 method + box breathing.","health"),
 ("Tenant rights summary","https://example.org/tenant-rights","What the landlord can and cannot do on rent increases.","housing"),
 ("Work permit FAQ (official)","https://example.gov/permit-faq","Official FAQ; bookmarked the renewal section.","immigration"),
]
for title,url,summ,tag in LINKS:
    for k in range(7):
        dt=rdate(0.2,1.0)
        t=title if k==0 else f"{title} (ref {k})"
        page(os.path.join(L,"Links"),t,["life","link",tag],dt,summ,
             f"saved {url}", source=url); bump("life")

simple_page(L,"Life — Index",["life","index","moc"],rdate(0.9,1.0),
    "Personal life-admin & wellbeing.\n\n- "+link("Immigration — Work Permit Renewal Master Plan")+
    "\n- People: "+link(partner)+", "+link(lawyer)+", "+link(therapist)+", "+link(landlord)+", "+link(mechanic)+
    "\n- Routines tagged `routine`, journal tagged `psychology`."); bump("life")

# ─────────────────────────────────────────────────────────────────────────────
total = sum(count.values())
print(f"Generated {total} notes  -> work={count['work']} study={count['study']} life={count['life']}")
print(f"Vault: {VAULT}")
