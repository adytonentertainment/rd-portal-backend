"""
Music Agreement Analyzer Service
Comprehensive music contract analysis using Claude Sonnet 4.
Extracts structured terms across 6 categories with color-coded assessments
and detects 18 red flag patterns.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, Optional

import anthropic

logger = logging.getLogger(__name__)


# System prompt ported from MusicAgreementAnalyzer.js (lines 38-204)
SYSTEM_PROMPT = """You are an expert music industry contract analyst with 20+ years of experience at major labels, publishing companies, and artist management firms. You analyze agreements with the precision of a lawyer and the practical insight of a dealmaker.

CRITICAL RULES:
1. Extract terms ONLY from the document provided - never invent or assume
2. SEARCH THOROUGHLY before marking any term as "NOT_FOUND":
   - Check tables, schedules, appendices, and exhibits
   - Look for synonyms and related terms (see SYNONYM MAPPING below)
   - Consider alternative phrasings and legal terminology variations
   - Only mark "NOT_FOUND" after exhaustive search
3. If a term is not found after thorough search, mark it "NOT_FOUND" with color "GRAY"
4. Cite the specific clause/section/paragraph for every extracted term
5. Include brief quotes as evidence for red flags
6. Be nuanced - consider context, artist tier, and deal type when assessing
7. Acknowledge when terms are standard vs. genuinely problematic
8. If document appears to have extraction issues (garbled text, missing sections), note this in your assessment

## EXHAUSTIVE SEARCH PROTOCOL

Before marking ANY term as "NOT_FOUND", you MUST:

1. **Search all document sections**: Main body, schedules, exhibits, appendices, addendums, riders
2. **Check tables and structured data**: Financial terms often appear in tables/grids
3. **Look for implied terms**: If royalty rate is mentioned but not labeled, extract it
4. **Consider standard clauses**: Terms like "audit rights" may be in boilerplate sections
5. **Infer from context**:
   - If document says "Producer shall receive 50% of net receipts", royalty_rate = "50% of net receipts"
   - If "worldwide rights" mentioned, territory = "Worldwide"
   - If no termination clause, duration may be "perpetual" or "life of copyright"
6. **Check for negative space**: Absence of audit rights = RED FLAG, not NOT_FOUND

### CATEGORY-SPECIFIC SEARCH TERMS

**FINANCIAL TERMS** - Search for: 'fee', 'payment', 'compensation', 'consideration', dollar amounts ($), currency symbols (£, €), percentage signs (%), 'per', 'at source', 'net', 'gross', 'points'

**RIGHTS TERMS** - Search for: 'grant', 'license', 'assign', 'transfer', 'worldwide', 'territory', 'term', 'duration', 'perpetuity', 'exclusive', 'non-exclusive', 'all media', 'throughout the universe'

**LEGAL TERMS** - Search for: 'indemnify', 'hold harmless', 'audit', 'inspect', 'examine books', 'warranty', 'representation', 'covenant', 'limitation of liability'

**CREDIT TERMS** - Search for: 'credit', 'attribution', 'name', 'billing', 'liner notes', 'metadata', 'acknowledgment'

**ADMINISTRATIVE TERMS** - Search for: 'statement', 'accounting', 'notice', 'amendment', 'modification', 'assignment', 'governing law', 'jurisdiction'

**PUBLISHING TERMS** - Search for: 'composition', 'mechanical', 'synchronization', 'sync', 'controlled composition', 'performance', 'ASCAP', 'BMI', 'SESAC', 'Harry Fox'

### HANDLING EXTERNAL REFERENCES

If you find a term but it references an external document (e.g., "as defined in Schedule A" but Schedule A is missing, or "per the Recording Agreement" which is not provided):
- Set value to "Referenced in [clause] but details not provided in this document"
- Set color to "YELLOW"
- Set assessment to "Term referenced but specific details are in an external document not provided"
- Do NOT mark as NOT_FOUND

### QUALITY-AWARE MARKING

If extraction_quality < 70 and you cannot find a term:
- Consider whether the term might exist but wasn't extracted properly
- Add extraction_issue: true to the term object
- Prefer "extraction_issue: true" over "NOT_FOUND" when document quality is poor

## WHEN TO USE "NOT_FOUND"

ONLY mark a term "NOT_FOUND" if:
- You've searched the entire document thoroughly
- The term is not implied or derivable from other clauses
- There's no industry-standard default that applies
- The document quality is good (extraction_quality > 70)

If extraction quality < 70, prefer marking terms with extraction_issue: true rather than NOT_FOUND.

## INFERENCE RULES

You MAY infer terms when:
- **Duration**: No termination clause = "Perpetual" or "Life of copyright" (mark YELLOW/RED based on context)
- **Territory**: No territory restriction = "Worldwide" (mark YELLOW)
- **Audit Rights**: Complete absence = "No audit rights specified" (mark RED, trigger RF03)
- **Royalty Base**: If rate given but base unclear, state "Percentage specified but base unclear" (mark YELLOW)
- **Payment Threshold**: No threshold mentioned = "No minimum threshold" (mark GREEN)
- **Escalation**: No escalation clause = "No escalation provisions" (mark YELLOW)

NEVER infer financial amounts (advances, rates) - these must be explicit.

## AGREEMENT-TYPE TERM RELEVANCE

Different agreement types have different applicable terms. For terms that are NOT APPLICABLE to the agreement type being analyzed, mark them as "N/A" with color "GRAY" and assessment explaining why (e.g., "Not applicable to publishing agreements").

**PUBLISHING_DEAL** - N/A terms:
- soundexchange (SoundExchange is for master recordings, not compositions)
- sync_share (this is already covered by sync_license in publishing section)
- rerecording_restriction (master recording concept, not publishing)

**PRODUCER_AGREEMENT** - All 52 terms are applicable

**RECORDING_CONTRACT** - N/A terms (unless publishing is bundled):
- controlled_composition (publishing-specific)
- mechanical_rates (publishing-specific)
- administration (publishing-specific)

**DISTRIBUTION_DEAL** - N/A terms:
- composition_ownership, controlled_composition, mechanical_rates, samples_clearance (all publishing terms)
- rerecording_restriction (label-artist concept, not distribution)

**SYNC_LICENSE** - N/A terms:
- escalation, recoupment, soundexchange (ongoing royalty concepts not typical in sync)
- rerecording_restriction (not relevant to sync licensing)

**MANAGEMENT_CONTRACT** - N/A terms:
- Most financial terms except commission structure (advance, royalty_rate, escalation, etc.)
- Most rights terms (these are managed, not directly licensed)
- Most publishing terms

For N/A terms:
- Set value to "N/A"
- Set color to "GRAY"
- Set assessment to "Not applicable to [agreement_type] - [brief reason]"
- Set industry_standard to "N/A"

## SYNONYM MAPPING - Check all variations before marking NOT_FOUND:

FINANCIAL TERMS:
- "advance" = "upfront payment", "fee", "initial payment", "signing bonus", "consideration", "sum payable upon execution"
- "royalty" = "percentage", "points", "share of revenue", "participation", "backend", "net receipts share"
- "recoupment" = "recoup", "recover", "offset", "deduct from royalties"
- "payment threshold" = "minimum payment", "accounting threshold", "de minimis", "floor"
- "escalation" = "increase", "step-up", "tier", "bonus rate", "accelerated rate"

RIGHTS TERMS:
- "duration" = "term", "period", "life of copyright", "perpetuity", "in perpetuity"
- "territory" = "worldwide", "universe", "all territories", "global", "throughout the world"
- "grant" = "license", "assign", "transfer", "convey", "vest"
- "remix rights" = "derivative works", "adaptations", "modifications", "re-recordings"

LEGAL TERMS:
- "indemnification" = "indemnify", "hold harmless", "defend", "liability"
- "audit rights" = "inspection rights", "examine books", "review records", "accountant access"
- "warranties" = "representations", "represents and warrants", "covenants"
- "termination" = "expiration", "cancel", "end of agreement", "cease"

## STEP 1: IDENTIFY AGREEMENT TYPE & CONTEXT

First, determine:
- Agreement type (PRODUCER_AGREEMENT, SAMPLE_CLEARANCE, RECORDING_CONTRACT, SYNC_LICENSE, PUBLISHING_DEAL, DISTRIBUTION_DEAL, MANAGEMENT_CONTRACT, WORK_FOR_HIRE, OTHER)
- Artist tier: EMERGING (< 100K monthly listeners), MID_LEVEL (100K-1M), ESTABLISHED (1M+), MAJOR (10M+)
- Deal context: indie label, major label, direct-to-artist, production company

## STEP 2: EXTRACT ALL TERMS

You MUST return ALL fields in ALL 6 sections with this structure:
{
  "value": "exact value from document or NOT_FOUND",
  "clause": "Section/Clause reference or null",
  "color": "RED | YELLOW | GREEN | GRAY",
  "assessment": "1-2 sentence explanation",
  "industry_standard": "what's typical"
}

### SECTION 1: FINANCIAL (financial)
Fields: advance, advance_structure, royalty_rate, royalty_calculation, royalty_base, payment_threshold, escalation, recoupment, sync_share, soundexchange

### SECTION 2: RIGHTS (rights)
Fields: grant_type, duration, territory, media_scope, remix_rights, name_likeness, assignment, rerecording_restriction

### SECTION 3: CREDIT (credit)
Fields: credit_format, credit_placement, credit_remedy, likeness_approval

### SECTION 4: LEGAL (legal)
Fields: warranties, indemnification, indemnity_withholding, audit_rights, objection_period, litigation_deadline, breach_cure, governing_law

### SECTION 5: ADMINISTRATIVE (administrative)
Fields: accounting_frequency, payment_timing, notices, amendment, assignment_rights, termination

### SECTION 6: PUBLISHING (publishing)
Fields: composition_ownership, controlled_composition, sync_license, administration, mechanical_rates, samples_clearance

## STEP 3: COLOR CODING

GRAY: Term not found or references missing external document
RED: Triggers red flag condition (fraction royalty, no audit rights, zero net advance, etc.)
YELLOW: Within normal market range
GREEN: Exceeds market standard favorably

### RED FLAG CONDITIONS
- advance: Net = $0 after deductions
- royalty_calculation: Uses "fraction", "applicable fraction", divides by artist rate
- audit_rights: NOT FOUND (absence = RED)
- indemnity_withholding: Unlimited, no cap
- escalation: Explicitly "excluded"
- recoupment: Multiple gates
- objection_period: < 1 year or tied to unknown deadline

### SPECIAL HANDLING FOR COMMON FALSE NEGATIVES

- **Audit Rights**: If you cannot find explicit audit rights language, mark as "No audit rights specified" (RED) and trigger RF03
- **Recoupment**: If advance mentioned but no recoupment terms, mark as "Standard recoupment assumed" (YELLOW)
- **Territory**: If no territory restriction, mark as "Worldwide" (YELLOW) not NOT_FOUND
- **Duration**: If no end date, mark as "Perpetual" or "Life of copyright" (RED/YELLOW) not NOT_FOUND

## STEP 4: RED FLAG DETECTION

Identify these 18 patterns (RF01-RF18):

CRITICAL: RF01 Fraction Royalty, RF02 Blind External Reference, RF03 No Audit Rights, RF04 Unlimited Indemnity Withholding, RF05 Net Zero Advance, RF06 Double Recoupment Gate

HIGH: RF07 Stacked Deductions, RF08 Sync via Fraction, RF09 AV Reduction

MEDIUM: RF10 Escalation Excluded, RF11 Short Objection Period, RF12 High Payment Threshold, RF13 Unlimited Remix Rights, RF14 Pro-Rata Compilation, RF15 Reversionary Rights Waived, RF16 Video Recoupment Gate, RF17 Services Bundled, RF18 Unknown Deadlines

## STEP 5: OUTPUT JSON

Return this exact structure (ALL fields required):

{
  "meta": {"document_hash": "[first 50 chars]", "analysis_date": "[ISO date]"},
  "agreement": {
    "type": "PRODUCER_AGREEMENT",
    "parties": {
      "party_a": {"name": "", "role": ""},
      "party_b": {"name": "", "role": ""},
      "artist": "", "distributor": "", "track_or_project": ""
    },
    "effective_date": "",
    "context": {"artist_tier": "", "deal_type": "", "has_legal_representation": true}
  },
  "overall_assessment": {
    "rating": "FAVORABLE|NEUTRAL|UNFAVORABLE|HIGHLY_UNFAVORABLE",
    "rating_explanation": "",
    "red_count": 0, "yellow_count": 0, "green_count": 0, "gray_count": 0,
    "critical_flags": 0, "high_flags": 0, "medium_flags": 0,
    "summary": ""
  },
  "terms": {
    "financial": {
      "advance": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "advance_structure": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "royalty_rate": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "royalty_calculation": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "royalty_base": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "payment_threshold": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "escalation": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "recoupment": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "sync_share": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "soundexchange": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""}
    },
    "rights": {
      "grant_type": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "duration": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "territory": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "media_scope": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "remix_rights": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "name_likeness": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "assignment": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "rerecording_restriction": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""}
    },
    "credit": {
      "credit_format": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "credit_placement": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "credit_remedy": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "likeness_approval": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""}
    },
    "legal": {
      "warranties": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "indemnification": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "indemnity_withholding": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "audit_rights": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "objection_period": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "litigation_deadline": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "breach_cure": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "governing_law": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""}
    },
    "administrative": {
      "accounting_frequency": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "payment_timing": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "notices": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "amendment": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "assignment_rights": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "termination": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""}
    },
    "publishing": {
      "composition_ownership": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "controlled_composition": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "sync_license": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "administration": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "mechanical_rates": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""},
      "samples_clearance": {"value": "", "clause": "", "color": "", "assessment": "", "industry_standard": ""}
    }
  },
  "red_flags": [
    {"id": "RF01", "name": "", "severity": "CRITICAL|HIGH|MEDIUM", "clause": "", "quote": "", "impact": "", "recommendation": ""}
  ],
  "favorable_terms": [
    {"term": "", "value": "", "clause": "", "why_favorable": ""}
  ],
  "negotiation_priorities": [
    {"priority": 1, "term": "", "issue": "", "current": "", "target": "", "impact": "CRITICAL|HIGH|MEDIUM|LOW", "achievability": ""}
  ],
  "financial_projection": {
    "scenario": "", "estimated_advance": "", "estimated_recording_royalties": "",
    "estimated_sync_income": "", "estimated_soundexchange": "", "estimated_publishing": "",
    "key_insight": ""
  },
  "comparison_notes": ""
}"""


SECTION_TITLES = {
    "financial": "1.1 Financial Terms",
    "rights": "1.2 Rights Granted",
    "credit": "1.3 Credit & Attribution",
    "legal": "1.4 Legal Protections",
    "administrative": "1.5 Administrative Terms",
    "publishing": "1.6 Composition & Publishing",
}

SECTION_ORDER = ["financial", "rights", "credit", "legal", "administrative", "publishing"]

# Required term keys for each section
REQUIRED_TERM_KEYS = {
    "financial": [
        "advance", "advance_structure", "royalty_rate", "royalty_calculation",
        "royalty_base", "payment_threshold", "escalation", "recoupment",
        "sync_share", "soundexchange"
    ],
    "rights": [
        "grant_type", "duration", "territory", "media_scope", "remix_rights",
        "name_likeness", "assignment", "rerecording_restriction"
    ],
    "credit": [
        "credit_format", "credit_placement", "credit_remedy", "likeness_approval"
    ],
    "legal": [
        "warranties", "indemnification", "indemnity_withholding", "audit_rights",
        "objection_period", "litigation_deadline", "breach_cure", "governing_law"
    ],
    "administrative": [
        "accounting_frequency", "payment_timing", "notices", "amendment",
        "assignment_rights", "termination"
    ],
    "publishing": [
        "composition_ownership", "controlled_composition", "sync_license",
        "administration", "mechanical_rates", "samples_clearance"
    ],
}


class MusicAgreementAnalyzer:
    """
    Comprehensive music agreement analyzer using Claude Sonnet 4.
    Extracts structured terms, applies color coding, and detects red flags.
    """

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the analyzer with Anthropic API key.

        Args:
            api_key: Anthropic API key. If None, uses ANTHROPIC_API_KEY env var.
        """
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = "claude-sonnet-4-20250514"

    def analyze(
        self,
        agreement_text: str,
        options: Optional[Dict[str, Any]] = None,
        extraction_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Analyze a music agreement and extract structured information.

        Args:
            agreement_text: The full text of the agreement to analyze.
            options: Optional dict with artistTierHint and dealTypeHint.
            extraction_metadata: Optional dict with extraction quality info:
                - method: extraction method used (vision_api, pdfplumber, pypdf2, docx)
                - quality_score: 0-100 extraction quality score
                - warnings: list of extraction warnings
                - character_count: number of characters extracted

        Returns:
            Dict containing the full analysis with terms, red flags, etc.
        """
        options = options or {}
        extraction_metadata = extraction_metadata or {}
        artist_tier_hint = options.get("artistTierHint")
        deal_type_hint = options.get("dealTypeHint")

        # Generate document hash from first 50 characters
        doc_hash = re.sub(r"\s+", " ", agreement_text[:50]).strip()

        # Build user message
        user_message = f"""Analyze this music agreement. Return ONLY valid JSON, no markdown code blocks.

DOCUMENT HASH: {doc_hash}"""

        # Add extraction metadata context with specific guidance
        if extraction_metadata:
            method = extraction_metadata.get("method", "unknown")
            quality_score = extraction_metadata.get("quality_score", 100)
            warnings = extraction_metadata.get("warnings", [])
            char_count = extraction_metadata.get("character_count", len(agreement_text))

            user_message += f"""

EXTRACTION METADATA:
- Method: {method}
- Quality Score: {quality_score}/100
- Character Count: {char_count}
- Warnings: {', '.join(warnings) if warnings else 'None'}

EXTRACTION QUALITY GUIDANCE:
"""
            if quality_score < 50:
                user_message += """
⚠️ CRITICAL: Extraction quality is VERY LOW (<50/100)
- Terms may be present but garbled or incomplete
- Be EXTREMELY thorough when searching - check every section multiple times
- If you suspect a term exists but cannot locate it clearly, set extraction_issue: true
- Prefer marking extraction_issue over NOT_FOUND
- Look for partial matches and fragments that suggest term presence
"""
            elif quality_score < 70:
                user_message += """
⚠️ WARNING: Extraction quality is MODERATE (50-70/100)
- Some terms may be harder to find due to formatting issues
- Search thoroughly before marking NOT_FOUND
- If uncertain, set extraction_issue: true
- Check tables and structured sections carefully
"""
            else:
                user_message += """
✓ Extraction quality is GOOD (70+/100)
- Document text should be reliable
- Still search thoroughly - terms may be in appendices or exhibits
- Only mark NOT_FOUND after exhaustive search
"""

        if artist_tier_hint:
            user_message += f"\n\nCONTEXT HINT: Artist appears to be {artist_tier_hint} tier."
        if deal_type_hint:
            user_message += f"\nDEAL TYPE HINT: This appears to be a {deal_type_hint} deal."

        user_message += f"\n\n---BEGIN AGREEMENT---\n{agreement_text}\n---END AGREEMENT---"

        try:
            logger.info(f"Calling Claude API for agreement analysis (model: {self.model})")

            response = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                temperature=0,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )

            # Verify response.content is a non-empty list with a text block
            if not response.content or not isinstance(response.content, list) or len(response.content) == 0:
                logger.warning("Anthropic response has empty or missing content")
                return {
                    "parse_error": "Empty response from Anthropic API",
                    "text_preview": agreement_text[:1000] if agreement_text else None,
                }

            first_block = response.content[0]
            if not hasattr(first_block, "text") or not first_block.text:
                logger.warning(f"Anthropic response first block has no text attribute or text is empty. Block type: {type(first_block).__name__}")
                return {
                    "parse_error": "Response did not contain expected text content",
                    "text_preview": agreement_text[:1000] if agreement_text else None,
                }

            result = self._parse_response(first_block.text)

            # Validate and ensure all required fields are present
            result = self._validate_result(result, doc_hash)

            # Post-process to catch false negatives and apply inference rules
            result = self._post_process_terms(result, agreement_text)

            # Log NOT_FOUND terms for debugging
            not_found_terms = []
            for section_name, section_data in result.get("terms", {}).items():
                for term_key, term_data in section_data.items():
                    if isinstance(term_data, dict) and term_data.get("value") == "NOT_FOUND":
                        not_found_terms.append(f"{section_name}.{term_key}")

            if not_found_terms:
                logger.warning(
                    f"Analysis marked {len(not_found_terms)} terms as NOT_FOUND: {', '.join(not_found_terms[:10])}"
                    + (f" and {len(not_found_terms) - 10} more" if len(not_found_terms) > 10 else "")
                )

            # Store the full text for re-parsing
            result["full_text"] = agreement_text
            result["text_preview"] = agreement_text[:500] if agreement_text else None

            logger.info(
                f"Agreement analysis complete: {len(result.get('red_flags', []))} red flags, "
                f"rating={result.get('overall_assessment', {}).get('rating', 'N/A')}"
            )

            return result

        except anthropic.APIError as e:
            logger.error(f"Anthropic API error during analysis: {e}")
            return {
                "error": str(e),
                "parse_error": "API error during analysis",
                "text_preview": agreement_text[:1000] if agreement_text else None,
            }
        except Exception as e:
            logger.error(f"Error during agreement analysis: {e}", exc_info=True)
            return {
                "error": str(e),
                "parse_error": "Failed to analyze agreement",
                "text_preview": agreement_text[:1000] if agreement_text else None,
            }

    def _parse_response(self, text: str) -> Dict[str, Any]:
        """
        Parse the JSON response from Claude, handling markdown code blocks.

        Args:
            text: Raw response text from Claude.

        Returns:
            Parsed JSON as a dictionary.
        """
        json_str = text

        # Handle markdown code blocks
        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if json_match:
            json_str = json_match.group(1)

        # Find the JSON object
        object_match = re.search(r"\{[\s\S]*\}", json_str)
        if object_match:
            json_str = object_match.group(0)

        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"Initial JSON parse failed: {e}, attempting repair...")

            # Attempt repair: fix unclosed braces
            open_braces = json_str.count("{") - json_str.count("}")
            open_brackets = json_str.count("[") - json_str.count("]")

            repaired = json_str.rstrip()
            # Remove trailing comma
            repaired = re.sub(r",\s*$", "", repaired)

            if open_brackets > 0:
                repaired += "]" * open_brackets
            if open_braces > 0:
                repaired += "}" * open_braces

            try:
                result = json.loads(repaired)
                logger.info("JSON repair successful")
                return result
            except json.JSONDecodeError:
                logger.error(f"JSON repair failed, returning error dict")
                raise

    def _count_colors(self, terms: Dict[str, Any]) -> Dict[str, int]:
        """
        Count occurrences of each color across all term sections.

        Args:
            terms: The terms dictionary from the analysis.

        Returns:
            Dict with counts for RED, YELLOW, GREEN, GRAY.
        """
        counts = {"RED": 0, "YELLOW": 0, "GREEN": 0, "GRAY": 0}

        def count_section(section: Dict[str, Any]) -> None:
            if not section or not isinstance(section, dict):
                return
            for term in section.values():
                if isinstance(term, dict) and term.get("color"):
                    color = term["color"]
                    if color in counts:
                        counts[color] += 1

        if not terms or not isinstance(terms, dict):
            return counts

        for section in terms.values():
            count_section(section)

        return counts

    def _validate_result(self, result: Dict[str, Any], doc_hash: str) -> Dict[str, Any]:
        """
        Validate and ensure all required fields are present in the parsed result.

        Ensures meta, overall_assessment, and all term sections have required fields
        with defaults when missing, so downstream callers always receive a complete payload.

        Args:
            result: The parsed result from Claude.
            doc_hash: The document hash (first 50 chars of agreement).

        Returns:
            Validated result with all required fields populated.
        """
        # Ensure meta exists with document_hash and analysis_date
        if "meta" not in result:
            result["meta"] = {}
        if not result["meta"].get("document_hash"):
            result["meta"]["document_hash"] = doc_hash
        if not result["meta"].get("analysis_date"):
            result["meta"]["analysis_date"] = datetime.now().isoformat()

        # Ensure terms exists
        if "terms" not in result:
            result["terms"] = {}

        # Ensure all six sections exist and have all required term keys
        default_term = {
            "value": "NOT_FOUND",
            "clause": None,
            "color": "GRAY",
            "assessment": "",
            "industry_standard": "",
            "extraction_issue": False  # Set to True if term might exist but extraction failed
        }

        for section_name, required_keys in REQUIRED_TERM_KEYS.items():
            if section_name not in result["terms"]:
                result["terms"][section_name] = {}

            section = result["terms"][section_name]
            for key in required_keys:
                if key not in section:
                    section[key] = default_term.copy()
                else:
                    # Ensure existing terms have all required sub-fields
                    term = section[key]
                    if not isinstance(term, dict):
                        section[key] = default_term.copy()
                    else:
                        for field, default_value in default_term.items():
                            if field not in term:
                                term[field] = default_value

        # Ensure overall_assessment exists
        if "overall_assessment" not in result:
            result["overall_assessment"] = {}

        # Recompute color counts from terms
        counts = self._count_colors(result.get("terms", {}))
        result["overall_assessment"]["red_count"] = counts["RED"]
        result["overall_assessment"]["yellow_count"] = counts["YELLOW"]
        result["overall_assessment"]["green_count"] = counts["GREEN"]
        result["overall_assessment"]["gray_count"] = counts["GRAY"]

        # Recompute red flag severity counts
        red_flags = result.get("red_flags", [])
        result["overall_assessment"]["critical_flags"] = sum(
            1 for rf in red_flags if rf.get("severity") == "CRITICAL"
        )
        result["overall_assessment"]["high_flags"] = sum(
            1 for rf in red_flags if rf.get("severity") == "HIGH"
        )
        result["overall_assessment"]["medium_flags"] = sum(
            1 for rf in red_flags if rf.get("severity") == "MEDIUM"
        )

        return result

    def _post_process_terms(self, result: Dict[str, Any], agreement_text: str) -> Dict[str, Any]:
        """
        Post-process terms to catch common false negatives and apply inference rules.
        Also marks N/A terms based on agreement type.

        Args:
            result: The validated result from Claude
            agreement_text: Original agreement text for re-checking

        Returns:
            Result with post-processed terms
        """
        terms = result.get("terms", {})
        text_lower = agreement_text.lower()

        # Detect agreement type for N/A term marking
        agreement_type = result.get("agreement", {}).get("type", "OTHER")

        # Term relevance mapping by agreement type
        # Keys are agreement types, values are dicts mapping section names to lists of N/A term keys
        TERM_RELEVANCE = {
            "PUBLISHING_DEAL": {
                "financial": ["soundexchange", "sync_share"],  # Master recording terms
                "rights": ["rerecording_restriction"],  # Master recording concept
            },
            "RECORDING_CONTRACT": {
                "publishing": ["controlled_composition", "mechanical_rates", "administration"],
            },
            "DISTRIBUTION_DEAL": {
                "publishing": ["composition_ownership", "controlled_composition", "mechanical_rates", "samples_clearance"],
                "rights": ["rerecording_restriction"],
            },
            "SYNC_LICENSE": {
                "financial": ["escalation", "recoupment", "soundexchange"],
                "rights": ["rerecording_restriction"],
            },
            "MANAGEMENT_CONTRACT": {
                "financial": ["advance", "royalty_rate", "royalty_calculation", "royalty_base",
                             "payment_threshold", "escalation", "recoupment", "sync_share", "soundexchange"],
                "rights": ["grant_type", "duration", "territory", "media_scope", "remix_rights",
                          "assignment", "rerecording_restriction"],
                "publishing": ["composition_ownership", "controlled_composition", "sync_license",
                              "administration", "mechanical_rates", "samples_clearance"],
            },
        }

        # 1. Audit Rights: If NOT_FOUND, check for absence and mark as RED
        if terms.get("legal", {}).get("audit_rights", {}).get("value") == "NOT_FOUND":
            # Check if audit/inspect/examine appears anywhere
            if not any(keyword in text_lower for keyword in ["audit", "inspect", "examine books", "review records"]):
                terms["legal"]["audit_rights"] = {
                    "value": "No audit rights specified",
                    "clause": None,
                    "color": "RED",
                    "assessment": "Document does not contain audit rights provisions. This is a critical red flag.",
                    "industry_standard": "Standard agreements include audit rights with 1-2 year lookback period",
                    "extraction_issue": False
                }
                # Ensure RF03 is triggered
                red_flags = result.get("red_flags", [])
                if not any(rf.get("id") == "RF03" for rf in red_flags):
                    red_flags.append({
                        "id": "RF03",
                        "name": "No Audit Rights",
                        "severity": "CRITICAL",
                        "clause": "N/A",
                        "quote": "No audit rights provisions found in document",
                        "impact": "Cannot verify accounting accuracy or detect underpayment",
                        "recommendation": "Add audit rights with 2-year lookback period and reasonable notice requirements"
                    })
                    result["red_flags"] = red_flags

        # 2. Territory: If NOT_FOUND, infer from context
        if terms.get("rights", {}).get("territory", {}).get("value") == "NOT_FOUND":
            if any(keyword in text_lower for keyword in ["worldwide", "universe", "all territories", "throughout the world"]):
                # Claude should have found this - mark extraction issue
                terms["rights"]["territory"]["extraction_issue"] = True
            elif "territory" not in text_lower and "jurisdiction" not in text_lower:
                # No territory restriction mentioned = worldwide
                terms["rights"]["territory"] = {
                    "value": "Worldwide (no territorial restrictions specified)",
                    "clause": None,
                    "color": "YELLOW",
                    "assessment": "No territorial restrictions found, implying worldwide rights grant",
                    "industry_standard": "Varies by deal type - worldwide is common for major deals",
                    "extraction_issue": False
                }

        # 3. Duration: If NOT_FOUND, check for perpetuity language
        if terms.get("rights", {}).get("duration", {}).get("value") == "NOT_FOUND":
            if any(keyword in text_lower for keyword in ["perpetuity", "perpetual", "life of copyright", "in perpetuity"]):
                terms["rights"]["duration"]["extraction_issue"] = True
            elif "term" not in text_lower and "duration" not in text_lower and "expiration" not in text_lower:
                # No termination = perpetual
                terms["rights"]["duration"] = {
                    "value": "Perpetual (no termination provisions specified)",
                    "clause": None,
                    "color": "RED",
                    "assessment": "No termination or duration provisions found, suggesting perpetual rights grant",
                    "industry_standard": "Standard agreements have defined terms (1-5 years) or reversion clauses",
                    "extraction_issue": False
                }

        # 4. Escalation: If NOT_FOUND, mark as no escalation
        if terms.get("financial", {}).get("escalation", {}).get("value") == "NOT_FOUND":
            if "escalat" not in text_lower and "increase" not in text_lower and "step" not in text_lower:
                terms["financial"]["escalation"] = {
                    "value": "No escalation provisions",
                    "clause": None,
                    "color": "YELLOW",
                    "assessment": "No royalty escalation provisions found in agreement",
                    "industry_standard": "Many agreements include escalations based on sales milestones",
                    "extraction_issue": False
                }

        # 5. Payment Threshold: If NOT_FOUND, mark as no threshold (GREEN)
        if terms.get("financial", {}).get("payment_threshold", {}).get("value") == "NOT_FOUND":
            if "threshold" not in text_lower and "minimum" not in text_lower:
                terms["financial"]["payment_threshold"] = {
                    "value": "No minimum payment threshold",
                    "clause": None,
                    "color": "GREEN",
                    "assessment": "No payment threshold found - all royalties should be paid regardless of amount",
                    "industry_standard": "Some agreements have $25-$100 thresholds to reduce admin costs",
                    "extraction_issue": False
                }

        # 6. Mark N/A terms based on agreement type
        # This converts NOT_FOUND to N/A for terms that aren't applicable to this agreement type
        na_terms_mapping = TERM_RELEVANCE.get(agreement_type, {})
        agreement_type_display = agreement_type.replace("_", " ").lower()

        for section_key, na_term_keys in na_terms_mapping.items():
            section_terms = terms.get(section_key, {})
            for term_key in na_term_keys:
                if term_key in section_terms:
                    current_value = section_terms[term_key].get("value", "")
                    # Only convert NOT_FOUND terms to N/A (don't override found values)
                    if current_value == "NOT_FOUND" or current_value == "N/A":
                        section_terms[term_key] = {
                            "value": "N/A",
                            "clause": None,
                            "color": "GRAY",
                            "assessment": f"Not applicable to {agreement_type_display}",
                            "industry_standard": "N/A",
                            "extraction_issue": False
                        }

        # Recompute color counts after post-processing
        counts = self._count_colors(result.get("terms", {}))
        result["overall_assessment"]["red_count"] = counts["RED"]
        result["overall_assessment"]["yellow_count"] = counts["YELLOW"]
        result["overall_assessment"]["green_count"] = counts["GREEN"]
        result["overall_assessment"]["gray_count"] = counts["GRAY"]

        # Recompute red flag severity counts
        red_flags = result.get("red_flags", [])
        result["overall_assessment"]["critical_flags"] = sum(
            1 for rf in red_flags if rf.get("severity") == "CRITICAL"
        )
        result["overall_assessment"]["high_flags"] = sum(
            1 for rf in red_flags if rf.get("severity") == "HIGH"
        )
        result["overall_assessment"]["medium_flags"] = sum(
            1 for rf in red_flags if rf.get("severity") == "MEDIUM"
        )

        return result


def analyze_agreement(
    text: str,
    api_key: str,
    artist_tier_hint: Optional[str] = None,
    deal_type_hint: Optional[str] = None,
    extraction_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Convenience function to analyze an agreement.

    Args:
        text: The agreement text to analyze.
        api_key: Anthropic API key.
        artist_tier_hint: Optional hint about artist tier.
        deal_type_hint: Optional hint about deal type.
        extraction_metadata: Optional dict with extraction quality info.

    Returns:
        Analysis result dictionary.
    """
    analyzer = MusicAgreementAnalyzer(api_key=api_key)
    options = {}
    if artist_tier_hint:
        options["artistTierHint"] = artist_tier_hint
    if deal_type_hint:
        options["dealTypeHint"] = deal_type_hint

    return analyzer.analyze(text, options, extraction_metadata)
