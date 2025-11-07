# LeadGen Workflow Guidebook

## Table of Contents

1. [Introduction & Purpose](#1-introduction--purpose)
2. [Complete Workflow Diagram](#2-complete-workflow-diagram)
3. [State Management](#3-state-management)
4. [Node-by-Node Breakdown](#4-node-by-node-breakdown)
5. [DotDB Subgraph Deep Dive](#5-dotdb-subgraph-deep-dive)
6. [Research Supervisor Integration](#6-research-supervisor-integration)
7. [Lead Data Structure](#7-lead-data-structure)
8. [Configuration](#8-configuration)
9. [API Clients](#9-api-clients)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Introduction & Purpose

### What is LeadGen?

LeadGen is a specialized workflow built on top of the Open Deep Research framework, designed specifically for **domain brokerage lead generation**. It takes a domain name as input and produces a structured list of potential buyers (leads) who might be interested in acquiring that domain.

### Key Objectives

1. **Domain Classification**: Categorize the input domain into one or more business categories (e.g., Generic Keywords, Informational, Category Killer, Geographic, etc.)

2. **Buyer Persona Generation**: Create tiered buyer personas (Tier 1-4) representing different types of companies that would be interested in the domain

3. **Dual Lead Generation**: Generate leads through two parallel paths:
   - **Web Research**: Uses AI-powered web search to find companies matching buyer personas
   - **DotDB Integration**: Uses keyword-based domain matching via DotDB API

4. **Lead Deduplication**: Merge and deduplicate leads from both sources based on normalized website URLs

5. **Structured Output**: Return a clean list of leads with detailed summaries, rationales, and metadata

### Use Cases

- Domain brokers identifying potential buyers for domains
- Domain investors researching market opportunities
- Companies looking to acquire relevant domains
- Market research for domain valuation

---

## 2. Complete Workflow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         START                                   │
│                    (domain_name input)                          │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│          classify_and_seed_supervisor                           │
│  • Classify domain into categories                              │
│  • Generate tiered buyer personas                               │
│  • Seed supervisor with classification output                   │
│  • Initialize research_brief                                    │
└────────────┬───────────────────────────────┬────────────────────┘
             │                               │
             │                               │
             ▼                               ▼
┌───────────────────────────┐   ┌──────────────────────────────┐
│   research_supervisor     │   │   dotdb_generate_leads       │
│   (Supervisor Subgraph)   │   │   (DotDB Subgraph)           │
│                           │   │                              │
│  • Delegates research     │   │  • Generate keywords         │
│    tasks to researchers   │   │  • Fetch domains from DotDB  │
│  • Finds companies        │   │  • Validate via Jina API     │
│    matching personas      │   │  • Extract leads from sites  │
│  • Extracts leads from    │   │                              │
│    research results       │   │                              │
└────────────┬──────────────┘   └──────────────┬───────────────┘
             │                                 │
             │                                 │
             └───────────┬─────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    dedupe_leads                                 │
│  • Normalize website URLs using tldextract                      │
│  • Deduplicate based on normalized domains                      │
│  • Keep leads with more information when duplicates found       │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                      get_leads                                  │
│  • Final deduplication pass                                     │
│  • Convert dict leads to Lead objects                           │
│  • Return clean, structured lead list                           │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                          END                                    │
│              (Final leads list output)                          │
└─────────────────────────────────────────────────────────────────┘
```

### Parallel Execution

The workflow executes two paths in parallel after classification:

1. **Research Supervisor Path**: Uses AI-powered web research to find companies
2. **DotDB Path**: Uses keyword-based domain matching

Both paths converge at `dedupe_leads` where results are merged and deduplicated.

---

## 3. State Management

### LeadGenState Structure

The main state for the LeadGen workflow is defined in `src/lead_gen/agent.py`:

```python
class LeadGenState(TypedDict, total=False):
    # Inputs
    domain_name: str

    # Intermediate
    classification_output: str

    # Supervisor context
    supervisor_messages: Annotated[list[MessageLikeRepresentation], override_reducer]
    research_brief: str

    # Research artifacts
    notes: Annotated[list[str], override_reducer]

    # Final structured leads
    leads: Annotated[list["Lead"], override_reducer]
```

**Location**: `src/lead_gen/agent.py:41-53`

### State Fields Explained

- **`domain_name`**: Input domain (e.g., "covertcameras.com")
- **`classification_output`**: LLM-generated classification and buyer personas (markdown text)
- **`supervisor_messages`**: Message history for the research supervisor (uses `override_reducer` to replace, not append)
- **`research_brief`**: Research guidance (initially set to `classification_output`)
- **`notes`**: Research notes from supervisor and researchers (accumulated via `override_reducer`)
- **`leads`**: Final structured leads (accumulated from both paths, then deduplicated)

### Reducer Functions

**`override_reducer`**: Used for fields that should be replaced rather than appended:
- `supervisor_messages`: Replaced entirely when supervisor is seeded
- `leads`: Can be overridden during deduplication

**`operator.add`** (default): Used for fields that accumulate:
- `notes`: Appends new notes to existing list
- `leads`: Initially accumulates leads from both paths

### State Flow Through Workflow

```
Input: {domain_name: "covertcameras.com"}
  ↓
classify_and_seed_supervisor
  → {classification_output, supervisor_messages, research_brief}
  ↓
[Parallel Execution]
  ├─ research_supervisor → {notes, leads}
  └─ dotdb_generate_leads → {leads}
  ↓
dedupe_leads
  → {leads: deduplicated_list}
  ↓
get_leads
  → {leads: final_clean_list}
  ↓
Output: {leads: [Lead, Lead, ...]}
```

---

## 4. Node-by-Node Breakdown

### 4.1 classify_and_seed_supervisor

**Location**: `src/lead_gen/agent.py:70-114`

**Purpose**:
- Classify the input domain into business categories
- Generate tiered buyer personas
- Initialize the research supervisor with classification context

**Input State**:
```python
{
    "domain_name": "covertcameras.com"
}
```

**Processing Logic**:

1. **Domain Classification**:
   - Uses `classification_and_buyers_prompt` from `classify_prompts.py`
   - LLM analyzes domain against 11 category classification guide
   - Generates classification summary and tiered buyer personas (Tier 1-4)

2. **Supervisor Seeding**:
   - Creates `leadgen_supervisor_prompt` (customized supervisor instructions)
   - Seeds supervisor with:
     - System message: LeadGen-specific supervisor prompt
     - Human message: Classification output (as research brief)

3. **State Initialization**:
   - Sets `classification_output` to LLM response
   - Sets `research_brief` to classification output
   - Overrides `supervisor_messages` with initial context

**Output State**:
```python
{
    "classification_output": "✅ Classification: Category 6: Product/Service...",
    "research_brief": "✅ Classification: Category 6: Product/Service...",
    "supervisor_messages": {
        "type": "override",
        "value": [
            SystemMessage(content=leadgen_supervisor_prompt),
            HumanMessage(content=classification_output)
        ]
    }
}
```

**Key Code References**:
- Classification prompt: `src/lead_gen/classify_prompts.py:3-43`
- Classification guide: `src/lead_gen/classify_prompts.py:46-99`
- Supervisor prompt: `src/lead_gen/classify_prompts.py:102-162`

**Model Configuration**:
- Uses `research_model` from configuration
- Structured output not required (free-form text)
- Retry logic: `max_structured_output_retries`

---

### 4.2 research_supervisor

**Location**: `src/open_deep_research/deep_researcher.py:186-389` (supervisor subgraph)

**Purpose**:
- Orchestrate web research to find companies matching buyer personas
- Delegate research tasks to parallel researchers
- Extract leads from research findings

**Input State** (from LeadGen context):
```python
{
    "supervisor_messages": [SystemMessage, HumanMessage],  # Seeded by classify_and_seed_supervisor
    "research_brief": "Classification and buyer personas...",
    "notes": [],
    "leads": []
}
```

**Processing Logic**:

The supervisor subgraph operates in a loop:

1. **Supervisor Node** (`supervisor`):
   - Analyzes research brief and current findings
   - Uses tools:
     - `think_tool`: Strategic reflection
     - `ConductResearch`: Delegate research tasks
     - `ResearchComplete`: Signal completion
   - Generates tool calls based on buyer personas

2. **Supervisor Tools Node** (`supervisor_tools`):
   - Executes tool calls from supervisor
   - For `ConductResearch`: Spawns researcher subgraph
   - For `think_tool`: Continues conversation
   - For `ResearchComplete`: Exits loop

3. **Researcher Subgraph** (spawned per `ConductResearch`):
   - Receives specific research topic (e.g., "Find companies that sell surveillance cameras")
   - Uses web search tools (Jina, Tavily, etc.) to find companies
   - Iterates with tool calls (max `max_react_tool_calls`)
   - Compresses research findings
   - **Extracts leads** from compressed research using `extract_leads_from_research`

4. **Lead Extraction**:
   - Function: `extract_leads_from_research` (`src/open_deep_research/deep_researcher.py:539-604`)
   - Takes compressed research and raw notes
   - Uses LLM with structured output (`LeadList` schema)
   - Extracts companies with websites, summaries, rationales, tiers

**Output State**:
```python
{
    "supervisor_messages": [...],  # Full conversation history
    "notes": ["Research note 1", "Research note 2", ...],
    "leads": [
        {
            "website": "https://example.com",
            "detailed_summary": "...",
            "rationale": "...",
            "tier": "Tier 1",
            "meta_data": {...}
        },
        ...
    ]
}
```

**Key Code References**:
- Supervisor subgraph: `src/open_deep_research/deep_researcher.py:377-389`
- Supervisor node: `src/open_deep_research/deep_researcher.py:186-233`
- Supervisor tools: `src/open_deep_research/deep_researcher.py:235-375`
- Lead extraction: `src/open_deep_research/deep_researcher.py:539-604`
- Researcher subgraph: `src/open_deep_research/deep_researcher.py:694-720`
- Compress research: `src/open_deep_research/deep_researcher.py:607-692`

**Configuration**:
- `max_researcher_iterations`: Max supervisor loop iterations (default: 5)
- `max_react_tool_calls`: Max tool calls per researcher (default: 4)
- `max_concurrent_research_units`: Max parallel researchers (default: 5)

---

### 4.3 dotdb_generate_leads

**Location**: `src/lead_gen/agent.py:305-325`

**Purpose**:
- Invoke the DotDB subgraph to generate leads from keyword-based domain matching
- Convert DotDB subgraph output to Lead objects

**Input State**:
```python
{
    "domain_name": "covertcameras.com",
    "classification_output": "✅ Classification: ..."
}
```

**Processing Logic**:

1. **Invoke DotDB Subgraph**:
   - Calls `dotdb_subgraph.ainvoke()` with domain and classification
   - DotDB subgraph executes full flow (see Section 5)

2. **Extract Leads**:
   - Gets `leads` from DotDB subgraph output
   - Converts dict leads to `Lead` Pydantic models

3. **Return Leads**:
   - Returns leads in LeadGen state format

**Output State**:
```python
{
    "leads": [
        Lead(
            website="https://example.com",
            detailed_summary="...",
            rationale="...",
            tier="Tier 1",
            meta_data={...}
        ),
        ...
    ]
}
```

**Key Code References**:
- DotDB subgraph invocation: `src/lead_gen/agent.py:308-311`
- Lead conversion: `src/lead_gen/agent.py:314-324`
- DotDB subgraph: `src/lead_gen/dotdb_subgraph.py:342-358`

---

### 4.4 dedupe_leads

**Location**: `src/lead_gen/agent.py:168-232`

**Purpose**:
- Deduplicate leads from both research paths based on normalized website URLs
- Keep leads with more information when duplicates are found

**Input State**:
```python
{
    "leads": [
        Lead(website="https://www.example.com", ...),  # From research_supervisor
        Lead(website="example.com", ...),              # From dotdb_generate_leads (duplicate)
        Lead(website="https://other.com", ...),         # Unique
        ...
    ]
}
```

**Processing Logic**:

1. **Normalize Websites**:
   - Uses `normalize_website()` function with `tldextract`
   - Extracts registered domain (domain + suffix)
   - Handles:
     - Protocols (http://, https://)
     - Subdomains (www, api, etc.)
     - Paths and query parameters
     - Two-part TLDs (co.uk, com.au, etc.)

2. **Deduplication Algorithm**:
   - Uses dictionary for O(1) lookup: `{normalized_domain: (index, Lead)}`
   - For each lead:
     - Normalize website URL
     - If not seen: Add to deduplicated list
     - If duplicate: Compare information richness
       - Keep lead with longer `detailed_summary`
       - Or keep lead with `meta_data` if other doesn't have it

3. **Override State**:
   - Uses `override_reducer` pattern to replace leads list

**Output State**:
```python
{
    "leads": {
        "type": "override",
        "value": [
            Lead(website="https://www.example.com", ...),  # Kept (more info)
            Lead(website="https://other.com", ...),         # Unique
            ...
        ]
    }
}
```

**Key Code References**:
- Deduplication logic: `src/lead_gen/agent.py:168-232`
- Normalize website: `src/lead_gen/agent.py:117-165`

**Example Normalization**:
```python
normalize_website("https://www.example.com/path?query=1")  # → "example.com"
normalize_website("http://api.example.co.uk")              # → "example.co.uk"
normalize_website("www.test.io")                            # → "test.io"
```

---

### 4.5 get_leads

**Location**: `src/lead_gen/agent.py:235-302`

**Purpose**:
- Final deduplication pass (handles serialization edge cases)
- Ensure leads are properly formatted as Lead objects
- Return clean, final lead list

**Input State**:
```python
{
    "leads": [
        Lead(...),  # Already deduplicated
        ...
    ]
}
```

**Processing Logic**:

1. **Handle Serialization**:
   - Converts dict leads (from serialized state) to Lead objects
   - Handles cases where state was serialized/deserialized (e.g., in LangSmith traces)

2. **Final Deduplication**:
   - Performs same deduplication logic as `dedupe_leads`
   - Ensures no duplicates after serialization/deserialization

3. **Override State**:
   - Uses `override_reducer` pattern to return final clean list

**Output State**:
```python
{
    "leads": {
        "type": "override",
        "value": [
            Lead(...),  # Final deduplicated, formatted leads
            ...
        ]
    }
}
```

**Key Code References**:
- Final deduplication: `src/lead_gen/agent.py:235-302`

**Why Two Deduplication Steps?**
- `dedupe_leads`: Handles initial merge from parallel paths
- `get_leads`: Handles edge cases from state serialization (especially in shared traces)

---

## 5. DotDB Subgraph Deep Dive

The DotDB subgraph is a complete workflow that generates leads from keyword-based domain matching. It's defined in `src/lead_gen/dotdb_subgraph.py`.

### 5.1 DotDB Subgraph Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                         START                                    │
│              (domain_name, classification_output)               │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│              generate_dotdb_keywords                           │
│  • Extract SLD from domain                                     │
│  • Use LLM to generate search keywords                        │
│  • Parse JSON_TOP_TIER or Top Tier bullets                    │
│  • Build variants (hyphenated, compact)                        │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│              fetch_dotdb_domains                                │
│  • Bulk API call for all keywords                              │
│  • Extract active domains from response                        │
│  • Filter by exact SLD match                                   │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│              check_jina_api                                     │
│  • Validate domains via Jina API                               │
│  • Concurrent requests (limit: 10)                             │
│  • Filter successful responses                                 │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│              jina_results_to_leads                             │
│  • For each successful Jina result                             │
│  • Use LLM to extract lead information                         │
│  • Return structured leads                                     │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                          END                                    │
│                    (leads list output)                          │
└─────────────────────────────────────────────────────────────────┘
```

### 5.2 DotDBState Structure

```python
class DotDBState(TypedDict):
    domain_name: str
    classification_output: Optional[str]
    generated_keywords: Annotated[List[str], lambda x, y: y]
    dotdb_domains: Annotated[List[str], lambda x, y: y]
    jina_results: Annotated[List[Dict[str, Any]], lambda x, y: y]
    active_domains: Annotated[List[str], lambda x, y: y]
    leads: Annotated[List[Dict[str, Any]], lambda x, y: y]
```

**Location**: `src/lead_gen/dotdb_subgraph.py:27-35`

### 5.3 Node Details

#### 5.3.1 generate_dotdb_keywords

**Location**: `src/lead_gen/dotdb_subgraph.py:51-154`

**Purpose**: Generate DotDB search keywords from domain using LLM

**Processing**:

1. **Extract SLD**:
   - Uses `extract_sld()` with `tldextract`
   - Example: "covertcameras.com" → "covertcameras"

2. **LLM Keyword Generation**:
   - Uses `DOTDB_KEYWORD_GEN_PROMPT` (strict prompt with rules)
   - LLM generates:
     - Top Tier keywords (5-12 items with scores)
     - Exact-Phrase Family
     - Geo-Expanded Family
     - Specialty/Niche Family
     - Brand/Org Structure Family
     - Marketing/Affiliate Family
     - Abbreviation/Shortform Family
     - Misspelling/Defensive Family (if applicable)

3. **Parse Output**:
   - Prefers machine-readable `JSON_TOP_TIER:` line
   - Fallback: Parse Top Tier bullet section
   - Final fallback: Use SLD if no keywords found

4. **Build Variants**:
   - For each keyword, creates variants:
     - Hyphenated: "covert camera" → "covert-camera"
     - Compact: "covert camera" → "covertcamera"
   - Limits to 80 total keywords

**Key Code References**:
- Keyword generation: `src/lead_gen/dotdb_subgraph.py:51-154`
- Prompt: `src/lead_gen/classify_prompts.py:164-264`
- SLD extraction: `src/lead_gen/dotdb_subgraph.py:45-48`

**Output**:
```python
{
    "generated_keywords": [
        "covertcamera",
        "covert-camera",
        "covertcamera-vehicles",
        ...
    ]
}
```

#### 5.3.2 fetch_dotdb_domains

**Location**: `src/lead_gen/dotdb_subgraph.py:157-196`

**Purpose**: Fetch active domains from DotDB API for generated keywords

**Processing**:

1. **Bulk API Call**:
   - Uses `DotDBClient.get_active_domains()` with all keywords
   - Single POST request to `/dotdb/getleads/bulk`
   - Returns domains grouped by keyword

2. **Flatten and Deduplicate**:
   - Flattens domains from all keywords into single list
   - Removes duplicates

3. **Exact SLD Filter**:
   - Extracts SLD from each domain
   - Keeps only domains whose SLD exactly matches a generated keyword
   - Filters out domains with different SLDs

**Key Code References**:
- Domain fetching: `src/lead_gen/dotdb_subgraph.py:157-196`
- DotDB client: `src/lead_gen/clients/dotdb_client.py:17-99`

**Output**:
```python
{
    "dotdb_domains": [
        "covertcamera.com",
        "covert-camera.io",
        "covertcameravehicles.net",
        ...
    ]
}
```

#### 5.3.3 check_jina_api

**Location**: `src/lead_gen/dotdb_subgraph.py:199-252`

**Purpose**: Validate domains via Jina API to ensure they're active websites

**Processing**:

1. **Concurrent Requests**:
   - Uses `asyncio.Semaphore` to limit concurrency (10 simultaneous)
   - Creates tasks for each domain

2. **Jina API Call**:
   - Uses `JinaClient.fetch_site_info()` for each domain
   - Extracts SLD and queries Jina search API
   - Uses `X-Site` header to target specific domain

3. **Response Processing**:
   - Checks if response is successful (`code == 200 && status == 20000`)
   - Extracts: title, url, content, description
   - Marks as success/failure

4. **Filter Active Domains**:
   - Returns only domains with successful Jina responses
   - Logs failures for debugging

**Key Code References**:
- Jina validation: `src/lead_gen/dotdb_subgraph.py:199-252`
- Jina client: `src/lead_gen/clients/jina_client.py:44-133`

**Output**:
```python
{
    "jina_results": [
        {
            "domain": "covertcamera.com",
            "title": "Covert Camera Systems",
            "url": "https://covertcamera.com",
            "content": "...",
            "description": "...",
            "success": True
        },
        ...
    ],
    "active_domains": [
        "covertcamera.com",
        "covert-camera.io",
        ...
    ]
}
```

#### 5.3.4 jina_results_to_leads

**Location**: `src/lead_gen/dotdb_subgraph.py:255-339`

**Purpose**: Extract structured leads from Jina API results using LLM

**Processing**:

1. **Filter Successful Results**:
   - Only processes domains with `success: True`

2. **LLM Lead Extraction** (per domain):
   - Creates prompt with:
     - Domain, URL, title, description, content (truncated to 4000 chars)
     - Classification guidance from `classification_output`
     - Few-shot examples
   - LLM extracts lead information:
     - `website`: Exact URL
     - `detailed_summary`: 2-4 sentences about the company
     - `rationale`: Why this is a relevant buyer
     - `tier`: Tier 1/2/3 classification
     - `meta_data`: Optional metadata (domain, title, signals, etc.)

3. **JSON Parsing**:
   - Parses LLM JSON response
   - Validates required fields
   - Skips invalid responses

**Key Code References**:
- Lead extraction: `src/lead_gen/dotdb_subgraph.py:255-339`

**Output**:
```python
{
    "leads": [
        {
            "website": "https://covertcamera.com",
            "detailed_summary": "Covert Camera Systems provides enterprise surveillance solutions...",
            "rationale": "Direct B2B provider of surveillance products/services aligned with category.",
            "tier": "Tier 1",
            "meta_data": {
                "domain": "covertcamera.com",
                "title": "Covert Camera Systems",
                "signals": {"active": True}
            }
        },
        ...
    ]
}
```

### 5.4 DotDB Subgraph Compilation

**Location**: `src/lead_gen/dotdb_subgraph.py:342-358`

```python
dotdb_builder = StateGraph(DotDBState)

dotdb_builder.add_node("generate_dotdb_keywords", generate_dotdb_keywords)
dotdb_builder.add_node("fetch_dotdb_domains", fetch_dotdb_domains)
dotdb_builder.add_node("check_jina_api", check_jina_api)
dotdb_builder.add_node("jina_results_to_leads", jina_results_to_leads)

dotdb_builder.add_edge(START, "generate_dotdb_keywords")
dotdb_builder.add_edge("generate_dotdb_keywords", "fetch_dotdb_domains")
dotdb_builder.add_edge("fetch_dotdb_domains", "check_jina_api")
dotdb_builder.add_edge("check_jina_api", "jina_results_to_leads")
dotdb_builder.add_edge("jina_results_to_leads", END)

dotdb_subgraph = dotdb_builder.compile()
```

---

## 6. Research Supervisor Integration

The research supervisor is a subgraph from the main Deep Research workflow, reused in LeadGen with customized prompts.

### 6.1 Supervisor Subgraph Structure

**Location**: `src/open_deep_research/deep_researcher.py:377-389`

```python
supervisor_builder = StateGraph(SupervisorState, config_schema=Configuration)

supervisor_builder.add_node("supervisor", supervisor)
supervisor_builder.add_node("supervisor_tools", supervisor_tools)

supervisor_builder.add_edge(START, "supervisor")
# Loop: supervisor ↔ supervisor_tools

supervisor_subgraph = supervisor_builder.compile()
```

### 6.2 LeadGen-Specific Customization

**Custom Supervisor Prompt**: `src/lead_gen/classify_prompts.py:102-162`

The `leadgen_supervisor_prompt` is customized for domain brokerage:

- Focuses on finding companies matching buyer personas
- Emphasizes finding real companies with official websites
- Instructs to prioritize companies that match classification categories
- Prohibits using acronyms/abbreviations in research questions

**Seeding Process**: `src/lead_gen/agent.py:98-113`

1. System message: `leadgen_supervisor_prompt`
2. Human message: `classification_output` (buyer personas)

### 6.3 Supervisor Loop Flow

```
supervisor
  ↓
  [Analyze research brief and findings]
  ↓
  [Generate tool calls: think_tool, ConductResearch, or ResearchComplete]
  ↓
supervisor_tools
  ↓
  [Execute tool calls]
  ↓
  ├─ think_tool → Continue to supervisor
  ├─ ConductResearch → Spawn researcher subgraph
  └─ ResearchComplete → Exit to END
```

### 6.4 Researcher Subgraph

**Location**: `src/open_deep_research/deep_researcher.py:694-720`

When supervisor calls `ConductResearch`, it spawns a researcher subgraph:

```
researcher
  ↓
  [Use tools: web_search, jina_read_url, think_tool, etc.]
  ↓
researcher_tools
  ↓
  [Execute tool calls]
  ↓
  ├─ More tool calls needed → Continue to researcher
  └─ Max iterations reached → compress_research
  ↓
compress_research
  ↓
  [Compress research findings]
  ↓
  [Extract leads via extract_leads_from_research]
  ↓
  Return: {compressed_research, raw_notes, leads}
```

### 6.5 Lead Extraction in Research

**Location**: `src/open_deep_research/deep_researcher.py:539-604`

After research is compressed, `extract_leads_from_research` is called:

1. **Input**: Compressed research text + raw notes
2. **LLM Extraction**: Uses structured output (`LeadList` schema)
3. **Output**: List of lead dictionaries

**Prompt Structure**:
```
Extract potential leads from the following research findings.
Focus on companies, organizations, or individuals who might be interested in the domain.

RESEARCH SUMMARY: {compressed_research}
RAW NOTES: {raw_notes}

For each lead, provide:
- website: The company's website URL
- detailed_summary: Why they'd be interested in the domain
- rationale: Your reasoning for this lead
- tier: Optional classification tier
- meta_data: Any additional relevant information

Only include real, verifiable leads with valid websites.
```

### 6.6 State Aggregation

Leads from multiple researchers are aggregated in supervisor state:

```python
class SupervisorState(TypedDict):
    supervisor_messages: Annotated[list[MessageLikeRepresentation], override_reducer]
    research_brief: str
    notes: Annotated[list[str], override_reducer] = []
    research_iterations: int = 0
    raw_notes: Annotated[list[str], override_reducer] = []
    leads: Annotated[list[dict], override_reducer] = []  # Accumulated from researchers
```

**Location**: `src/open_deep_research/state.py:74-82`

---

## 7. Lead Data Structure

### 7.1 Lead Model

**Location**: `src/lead_gen/agent.py:56-63`

```python
class Lead(BaseModel):
    website: str = Field(..., description="Canonical website or domain of the lead")
    detailed_summary: str = Field(..., description="Detailed, actionable summary of why this is a fit")
    rationale: str = Field(..., description="Short justification tying back to classification/buyer tiers")
    tier: Optional[str] = Field(None, description="Buyer or classification tier for this lead")
    meta_data: Optional[Dict[str, Any]] = Field(
        default=None, description="Optional metadata such as contact hints, geo, size"
    )
```

### 7.2 Field Descriptions

- **`website`**: Canonical URL or domain (e.g., "https://example.com" or "example.com")
- **`detailed_summary`**: 2-4 sentences explaining the company's business and why they'd want the domain
- **`rationale`**: 1-2 sentences tying the lead back to classification categories or buyer personas
- **`tier`**: Buyer tier classification (e.g., "Tier 1", "Tier 2", "Tier 3", "Strategic")
- **`meta_data`**: Optional dictionary with additional information:
  - `domain`: Domain name
  - `title`: Company name/title
  - `signals`: Activity signals (e.g., `{"active": True}`)
  - `geo`: Geographic information
  - `contact`: Contact hints
  - `size`: Company size indicators

### 7.3 LeadList Model

**Location**: `src/lead_gen/agent.py:66-67`

```python
class LeadList(BaseModel):
    leads: List[Lead] = Field(..., description="List of extracted leads from web search results")
```

Used for structured LLM output when extracting leads.

### 7.4 Example Lead

```python
Lead(
    website="https://acme-security.com",
    detailed_summary="Acme Security provides enterprise-grade surveillance systems, including IP cameras, VMS, and integration services for logistics and retail. Their offerings emphasize compliance, 24/7 monitoring, and on-site deployment support.",
    rationale="Direct B2B provider of surveillance products/services aligned with category.",
    tier="Tier 1",
    meta_data={
        "domain": "acme-security.com",
        "title": "Acme Security",
        "signals": {"active": True}
    }
)
```

---

## 8. Configuration

### 8.1 LeadGenConfiguration

**Location**: `src/lead_gen/configuration.py:7-11`

```python
class LeadGenConfiguration(BaseModel):
    scraper_url: str = Field(default_factory=lambda: os.getenv("SCRAPER_URL", "http://localhost:3000/api/"))
    dotdb_url: str = Field(default_factory=lambda: os.getenv("DOTDB_URL", "https://amp2-1.grayriver-ffcf7337.westus.azurecontainerapps.io"))
    jina_api_key: str = Field(default_factory=lambda: os.getenv("JINA_API_KEY", ""))
```

**Environment Variables**:
- `SCRAPER_URL`: URL for scraping API (default: `http://localhost:3000/api/`)
- `DOTDB_URL`: Base URL for DotDB API
- `JINA_API_KEY`: Jina AI API key

### 8.2 Configuration (from Open Deep Research)

LeadGen uses the main `Configuration` class from `open_deep_research.configuration`:

**Key Settings**:
- `research_model`: Model for research (default: `openai:gpt-4.1`)
- `research_model_max_tokens`: Max output tokens (default: 8000)
- `max_researcher_iterations`: Max supervisor iterations (default: 5)
- `max_react_tool_calls`: Max tool calls per researcher (default: 4)
- `max_concurrent_research_units`: Max parallel researchers (default: 5)
- `max_structured_output_retries`: Retry attempts (default: 3)
- `search_api`: Search API to use (TAVILY, JINA, ANTHROPIC, OPENAI, NONE)

**Location**: `src/open_deep_research/configuration.py:39-257`

### 8.3 Environment Variable Override

Configuration values can be overridden via environment variables (uppercase):

```bash
RESEARCH_MODEL=openai:gpt-4.1
RESEARCH_MODEL_MAX_TOKENS=6000
MAX_RESEARCHER_ITERATIONS=5
SEARCH_API=jina
```

**Priority Order**:
1. Environment variables (highest)
2. `RunnableConfig.configurable`
3. Default values (lowest)

**Location**: `src/open_deep_research/configuration.py:242-252`

---

## 9. API Clients

### 9.1 DotDBClient

**Location**: `src/lead_gen/clients/dotdb_client.py:5-99`

**Purpose**: Interact with DotDB API to fetch active domains for keywords

**Key Methods**:

#### `get_active_domains(keywords, site_status)`

**Parameters**:
- `keywords`: List of keywords to search (e.g., `["covertcamera", "marketingguru"]`)
- `site_status`: Site status filter (default: `"active"`)

**Returns**: Dictionary mapping keywords to lists of active domains:
```python
{
    "covertcamera": ["covertcamera.com", "covertcamera.io", ...],
    "marketingguru": ["marketingguru.net", ...],
    ...
}
```

**API Endpoint**: `POST {base_url}/dotdb/getleads/bulk`

**Request Format**:
```json
["covertcamera", "marketingguru", ...]
```

**Response Format**:
```json
{
    "covertcamera": {
        "matches": [
            {
                "name": "covertcamera",
                "site_status": {
                    "active_suffixes": [".com", ".io", ".net"]
                }
            }
        ]
    },
    ...
}
```

**Usage Example**:
```python
from lead_gen.clients.dotdb_client import DotDBClient
from lead_gen.configuration import LeadGenConfiguration

config = LeadGenConfiguration()
client = DotDBClient(config.dotdb_url)

domains = await client.get_active_domains(
    keywords=["covertcamera", "marketingguru"],
    site_status="active"
)
```

### 9.2 JinaClient

**Location**: `src/lead_gen/clients/jina_client.py:44-133`

**Purpose**: Interact with Jina AI API to fetch website information

**Key Methods**:

#### `fetch_site_info(domain)`

**Parameters**:
- `domain`: Domain name (e.g., `"covertcamera.com"`)

**Returns**: Jina API response dictionary:
```python
{
    "code": 200,
    "status": 20000,
    "data": [
        {
            "title": "Covert Camera Systems",
            "url": "https://covertcamera.com",
            "content": "...",
            "description": "..."
        }
    ]
}
```

**API Endpoint**: `GET https://s.jina.ai/?q={sld}`

**Headers**:
- `Authorization: Bearer {api_key}`
- `X-Engine: direct`
- `X-Site: {domain}`

**Helper Methods**:
- `is_success_response(response)`: Check if response is successful
- `get_error_message(response)`: Extract error message from failed response

**Usage Example**:
```python
from lead_gen.clients.jina_client import JinaClient
from lead_gen.configuration import LeadGenConfiguration

config = LeadGenConfiguration()
client = JinaClient(api_key=config.jina_api_key)

response = await client.fetch_site_info("covertcamera.com")
if JinaClient.is_success_response(response):
    data = response.get("data", [])
    if data:
        title = data[0].get("title")
        url = data[0].get("url")
```

### 9.3 ScraperClient

**Location**: `src/lead_gen/clients/scraping_client.py:5-25`

**Purpose**: Scrape company information from internal scraping API

**Key Methods**:

#### `get_company_info(company_domain)`

**Parameters**:
- `company_domain`: Company domain (e.g., `"swiggy.com"`)

**Returns**: Company information dictionary or `None`:
```python
{
    "name": "Swiggy",
    "description": "...",
    "revenue": "...",
    "employee_size": "...",
    "socials": {...},
    ...
}
```

**API Endpoint**: `POST {scraper_url}/company/tracxn`

**Request Format**:
```json
{
    "companyDomain": "swiggy.com"
}
```

**Usage Example**:
```python
from lead_gen.clients.scraping_client import ScraperClient
from lead_gen.configuration import LeadGenConfiguration

config = LeadGenConfiguration()
client = ScraperClient(config)

company_info = await client.get_company_info("swiggy.com")
```

**Note**: This client is available as a tool (`scraping_company_info`) in the research workflow but is not directly used in the main LeadGen flow.

---

## 10. Troubleshooting

### 10.1 Common Issues

#### Issue: No Leads Generated

**Symptoms**:
- Workflow completes successfully but `leads` list is empty

**Possible Causes**:
1. Classification didn't generate buyer personas
2. Research supervisor didn't find matching companies
3. DotDB keywords didn't match any domains
4. Jina API validation failed for all domains
5. Lead extraction LLM returned empty results

**Solutions**:
- Check `classification_output` in state - ensure buyer personas were generated
- Review `supervisor_messages` - check if supervisor found companies
- Check `generated_keywords` in DotDB state - ensure keywords were generated
- Review `jina_results` - check if domains were validated
- Check LLM responses in `jina_results_to_leads` - ensure leads were extracted

**Debug Steps**:
```python
# Check classification
print(state.get("classification_output"))

# Check supervisor research
print(state.get("notes"))
print(state.get("supervisor_messages")[-1].content)

# Check DotDB flow
dotdb_state = await dotdb_subgraph.ainvoke({"domain_name": "example.com"})
print(dotdb_state.get("generated_keywords"))
print(dotdb_state.get("dotdb_domains"))
print(dotdb_state.get("active_domains"))
```

#### Issue: Duplicate Leads

**Symptoms**:
- Same company appears multiple times in final leads

**Possible Causes**:
1. Website normalization failed (different URLs for same domain)
2. Deduplication logic bug
3. State serialization issue

**Solutions**:
- Check `normalize_website()` function - ensure it handles all URL variations
- Verify deduplication runs in both `dedupe_leads` and `get_leads`
- Check if leads have different website formats (http vs https, www vs non-www)

**Debug Steps**:
```python
from lead_gen.agent import normalize_website

# Test normalization
print(normalize_website("https://www.example.com"))
print(normalize_website("http://example.com"))
print(normalize_website("example.com"))
# All should return "example.com"
```

#### Issue: Token Limit Exceeded

**Symptoms**:
- `BadRequestError` with context length message
- Workflow fails during LLM calls

**Possible Causes**:
1. `research_model_max_tokens` too high
2. Classification output too long
3. Research findings too large

**Solutions**:
- Reduce `RESEARCH_MODEL_MAX_TOKENS` in `.env` (e.g., 6000)
- Check classification output length
- Reduce `max_researcher_iterations` to limit research scope

**Configuration**:
```bash
RESEARCH_MODEL_MAX_TOKENS=6000
MAX_RESEARCHER_ITERATIONS=3
```

#### Issue: DotDB API Errors

**Symptoms**:
- `RuntimeError` from DotDB client
- No domains returned from DotDB

**Possible Causes**:
1. DotDB API unavailable
2. Invalid `DOTDB_URL`
3. Network timeout

**Solutions**:
- Verify `DOTDB_URL` in environment variables
- Check DotDB API status
- Increase timeout in `DotDBClient` if needed

**Debug Steps**:
```python
from lead_gen.clients.dotdb_client import DotDBClient
from lead_gen.configuration import LeadGenConfiguration

config = LeadGenConfiguration()
print(f"DOTDB URL: {config.dotdb_url}")

client = DotDBClient(config.dotdb_url)
try:
    domains = await client.get_active_domains(["test"], "active")
    print(f"Success: {domains}")
except Exception as e:
    print(f"Error: {e}")
```

#### Issue: Jina API Validation Fails

**Symptoms**:
- All domains fail Jina validation
- `active_domains` list is empty

**Possible Causes**:
1. Invalid `JINA_API_KEY`
2. Jina API rate limiting
3. Domains are actually inactive/parked

**Solutions**:
- Verify `JINA_API_KEY` in environment variables
- Check Jina API rate limits
- Review `jina_results` to see error messages
- Reduce concurrency limit in `check_jina_api` (default: 10)

**Debug Steps**:
```python
from lead_gen.clients.jina_client import JinaClient
from lead_gen.configuration import LeadGenConfiguration

config = LeadGenConfiguration()
print(f"Jina API Key set: {bool(config.jina_api_key)}")

client = JinaClient(api_key=config.jina_api_key)
response = await client.fetch_site_info("example.com")
print(f"Success: {JinaClient.is_success_response(response)}")
if not JinaClient.is_success_response(response):
    print(f"Error: {JinaClient.get_error_message(response)}")
```

### 10.2 State Inspection

**In LangGraph Studio**:
1. Open workflow execution
2. Click on each node to inspect state
3. Check `leads` field at each step
4. Review `supervisor_messages` for research progress
5. Check `classification_output` for buyer personas

**Programmatic Inspection**:
```python
# Run workflow and inspect state
final_state = await leadgen_researcher.ainvoke(
    {"domain_name": "example.com"},
    config
)

# Check each field
print("Classification:", final_state.get("classification_output"))
print("Leads count:", len(final_state.get("leads", [])))
print("Notes count:", len(final_state.get("notes", [])))

# Inspect leads
for lead in final_state.get("leads", []):
    print(f"Lead: {lead.website} - {lead.tier}")
```

### 10.3 Performance Optimization

**Reduce Research Scope**:
- Lower `max_researcher_iterations` (default: 5)
- Lower `max_react_tool_calls` (default: 4)
- Lower `max_concurrent_research_units` (default: 5)

**Optimize DotDB Flow**:
- Limit `generated_keywords` (currently capped at 80)
- Reduce Jina concurrency (currently 10)

**Model Selection**:
- Use faster models for classification (e.g., `gpt-4.1-mini`)
- Use more capable models for research (e.g., `gpt-4.1`)

### 10.4 Logging

Enable debug logging to trace execution:

```python
import logging

logging.basicConfig(level=logging.DEBUG)
```

**Key Log Points**:
- `[dotdb]` prefix: DotDB subgraph operations
- `[jina_search]` prefix: Jina search operations
- `[jina_reader]` prefix: Jina reader operations

---

## Appendix: Code Reference Map

### Main Graph
- **Graph Definition**: `src/lead_gen/agent.py:328-353`
- **Entry Point**: `leadgen_researcher` (compiled graph)

### Nodes
- **classify_and_seed_supervisor**: `src/lead_gen/agent.py:70-114`
- **research_supervisor**: `src/open_deep_research/deep_researcher.py:377-389` (subgraph)
- **dotdb_generate_leads**: `src/lead_gen/agent.py:305-325`
- **dedupe_leads**: `src/lead_gen/agent.py:168-232`
- **get_leads**: `src/lead_gen/agent.py:235-302`

### DotDB Subgraph
- **Graph Definition**: `src/lead_gen/dotdb_subgraph.py:342-358`
- **generate_dotdb_keywords**: `src/lead_gen/dotdb_subgraph.py:51-154`
- **fetch_dotdb_domains**: `src/lead_gen/dotdb_subgraph.py:157-196`
- **check_jina_api**: `src/lead_gen/dotdb_subgraph.py:199-252`
- **jina_results_to_leads**: `src/lead_gen/dotdb_subgraph.py:255-339`

### Supervisor Integration
- **Supervisor Subgraph**: `src/open_deep_research/deep_researcher.py:377-389`
- **Supervisor Node**: `src/open_deep_research/deep_researcher.py:186-233`
- **Supervisor Tools**: `src/open_deep_research/deep_researcher.py:235-375`
- **Researcher Subgraph**: `src/open_deep_research/deep_researcher.py:694-720`
- **Compress Research**: `src/open_deep_research/deep_researcher.py:607-692`
- **Extract Leads**: `src/open_deep_research/deep_researcher.py:539-604`

### Data Models
- **LeadGenState**: `src/lead_gen/agent.py:41-53`
- **Lead**: `src/lead_gen/agent.py:56-63`
- **LeadList**: `src/lead_gen/agent.py:66-67`
- **DotDBState**: `src/lead_gen/dotdb_subgraph.py:27-35`

### Prompts
- **Classification Prompt**: `src/lead_gen/classify_prompts.py:3-43`
- **Classification Guide**: `src/lead_gen/classify_prompts.py:46-99`
- **LeadGen Supervisor Prompt**: `src/lead_gen/classify_prompts.py:102-162`
- **DotDB Keyword Generation Prompt**: `src/lead_gen/classify_prompts.py:164-264`

### Clients
- **DotDBClient**: `src/lead_gen/clients/dotdb_client.py:5-99`
- **JinaClient**: `src/lead_gen/clients/jina_client.py:44-133`
- **ScraperClient**: `src/lead_gen/clients/scraping_client.py:5-25`

### Configuration
- **LeadGenConfiguration**: `src/lead_gen/configuration.py:7-11`
- **Configuration** (main): `src/open_deep_research/configuration.py:39-257`

### Utilities
- **normalize_website**: `src/lead_gen/agent.py:117-165`
- **extract_sld**: `src/lead_gen/dotdb_subgraph.py:45-48`

---

## Conclusion

This guidebook provides a comprehensive overview of the LeadGen workflow, from domain classification to final lead generation. The workflow combines AI-powered web research with keyword-based domain matching to produce high-quality leads for domain brokerage.

### Key Takeaways

1. **Dual Path Architecture**: LeadGen uses two parallel paths (research supervisor and DotDB) to maximize lead discovery
2. **State Management**: Careful use of reducers ensures proper state accumulation and override
3. **Deduplication**: Two-stage deduplication ensures clean, unique leads
4. **Modularity**: DotDB subgraph and supervisor subgraph are reusable components
5. **Extensibility**: Easy to add new lead sources or modify extraction logic

### Next Steps

- Review the code references to understand implementation details
- Experiment with different configurations to optimize for your use case
- Monitor state transitions in LangGraph Studio for debugging
- Extend the workflow with additional lead sources or validation steps

For questions or contributions, refer to the main project documentation and codebase.
