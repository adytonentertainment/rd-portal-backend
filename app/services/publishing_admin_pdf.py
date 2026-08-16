import io
from datetime import datetime

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)


class PublishingAdminPDFGenerator:
    """Generates a legally conforming Publishing Administration Agreement PDF
    under California law with DocuSign anchor tags for signature placement."""

    ADMIN_COMPANY = "Adyton Entertainment S.L."
    ADMIN_ADDRESS = "Carrer Montapre 3, 17310 Lloret de Mar, Spain"

    def generate(self, form_data: dict) -> io.BytesIO:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            topMargin=60,
            bottomMargin=60,
            leftMargin=66,
            rightMargin=66,
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "AgreementTitle",
            parent=styles["Title"],
            fontSize=14,
            spaceAfter=4,
            alignment=1,
            fontName="Helvetica-Bold",
        )
        subtitle_style = ParagraphStyle(
            "Subtitle",
            parent=styles["Normal"],
            fontSize=9,
            spaceAfter=16,
            alignment=1,
            textColor="#555555",
        )
        heading_style = ParagraphStyle(
            "SectionHeading",
            parent=styles["Heading2"],
            fontSize=10,
            spaceBefore=14,
            spaceAfter=6,
            fontName="Helvetica-Bold",
        )
        body_style = ParagraphStyle(
            "AgreementBody",
            parent=styles["Normal"],
            fontSize=9,
            leading=13,
            spaceAfter=4,
        )
        indent_style = ParagraphStyle(
            "IndentBody",
            parent=body_style,
            leftIndent=20,
            spaceAfter=3,
        )
        bold_style = ParagraphStyle(
            "BoldBody",
            parent=body_style,
            fontName="Helvetica-Bold",
        )
        sig_style = ParagraphStyle(
            "Signature",
            parent=body_style,
            fontSize=9,
            leading=16,
        )

        legal_name = form_data.get("legalName", "")
        producer_name = form_data.get("producerName", "")
        email = form_data.get("email", "")
        address = form_data.get("address", "")
        city = form_data.get("city", "")
        state = form_data.get("state", "")
        zip_code = form_data.get("zip", "")
        country = form_data.get("country", "United States")
        term_type = form_data.get("termType", "3month")
        date_str = datetime.now().strftime("%B %d, %Y")

        full_address = f"{address}, {city}, {state} {zip_code}, {country}"

        # Term-specific language
        if term_type == "2year":
            term_paragraph = (
                'The initial term of this Agreement shall commence on the date of full execution '
                'by both parties and shall continue for a period of two (2) years thereafter '
                '(the "Initial Term"). Upon expiration of the Initial Term, this Agreement '
                'shall automatically renew for successive one (1) year periods (each a '
                '"Renewal Term"), unless either party delivers written notice of non-renewal '
                'to the other party no later than sixty (60) days prior to the expiration of '
                'the then-current term. The Initial Term and any Renewal Terms are collectively '
                'referred to as the "Term."'
            )
            term_summary = "2 years (auto-renew annually)"
        else:
            term_paragraph = (
                'The term of this Agreement shall commence on the date of full execution '
                'by both parties and shall continue for an initial period of thirty (30) days '
                '(the "Initial Term"). Upon expiration of the Initial Term, this Agreement '
                'shall automatically renew for successive thirty (30) day periods (each a '
                '"Renewal Term"), unless either Party delivers written notice of termination '
                'to the other Party no later than fifteen (15) days prior to the expiration of '
                'the then-current term. The Initial Term and any Renewal Terms are collectively '
                'referred to as the "Term."'
            )
            term_summary = "30-day rolling (auto-renew, either party may terminate)"

        story = []

        # Title
        story.append(Paragraph("PUBLISHING ADMINISTRATION AGREEMENT", title_style))
        story.append(Paragraph(f"Effective Date: {date_str}", subtitle_style))

        # Parties
        story.append(Paragraph("PARTIES", heading_style))
        story.append(Paragraph(
            f'This Publishing Administration Agreement (this "Agreement") is entered into '
            f'as of the date last signed below (the "Effective Date"), by and between:',
            body_style,
        ))
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph(
            f'<b>{self.ADMIN_COMPANY}</b>, a Spanish sociedad limitada (S.L.), '
            f'with its principal place of business at {self.ADMIN_ADDRESS} '
            f'(hereinafter "Administrator");',
            indent_style,
        ))
        story.append(Spacer(1, 0.04 * inch))
        story.append(Paragraph("and", indent_style))
        story.append(Spacer(1, 0.04 * inch))
        story.append(Paragraph(
            f'<b>{legal_name}</b>, professionally known as "{producer_name}", '
            f'an individual residing at {full_address}, '
            f'with the email address {email} '
            f'(hereinafter "Writer").',
            indent_style,
        ))
        story.append(Paragraph(
            "Administrator and Writer are each referred to herein as a "
            '"Party" and collectively as the "Parties."',
            body_style,
        ))

        # Recitals
        story.append(Paragraph("RECITALS", heading_style))
        story.append(Paragraph(
            "<b>WHEREAS</b>, Writer is the owner and/or controller of certain musical "
            "compositions and sound recordings and the copyrights and publishing rights therein; and",
            body_style,
        ))
        story.append(Paragraph(
            "<b>WHEREAS</b>, Writer desires to retain Administrator to administer "
            "Writer's music publishing catalog, collect royalties, and perform related services "
            "on Writer's behalf, subject to the terms and conditions set forth herein; and",
            body_style,
        ))
        story.append(Paragraph(
            "<b>WHEREAS</b>, Administrator desires to provide such publishing administration "
            "services to Writer on the terms set forth herein;",
            body_style,
        ))
        story.append(Paragraph(
            "<b>NOW, THEREFORE</b>, in consideration of the mutual covenants, promises, and "
            "agreements contained herein, and for other good and valuable consideration, "
            "the receipt and sufficiency of which are hereby acknowledged, the Parties agree as follows:",
            body_style,
        ))

        # Section 1: Grant of Rights
        story.append(Paragraph("1. GRANT OF RIGHTS", heading_style))
        story.append(Paragraph(
            '1.1 Writer hereby grants to Administrator the exclusive right during the Term '
            'to administer, on Writer\'s behalf, all musical compositions and underlying works '
            'written, co-written, owned, or controlled by Writer, whether now in existence or '
            'hereafter created during the Term (collectively, the "Compositions"), including '
            'without limitation the right to:',
            body_style,
        ))
        grant_items = [
            ("(a)", "Register and re-register the Compositions with all applicable performing "
             "rights organizations (\"PROs\"), mechanical rights organizations (including but "
             "not limited to The Mechanical Licensing Collective), digital service providers, "
             "and collection societies worldwide;"),
            ("(b)", "Collect all income and royalties generated by the Compositions from any "
             "and all sources worldwide, including but not limited to mechanical royalties, "
             "performance royalties, synchronization license fees, digital streaming royalties, "
             "print royalties, micro-sync fees, and any other income derived from the exploitation "
             "of the Compositions;"),
            ("(c)", "Issue and grant non-exclusive licenses for the mechanical reproduction and "
             "digital distribution of the Compositions. Synchronization licenses shall require "
             "Writer's prior written approval, which shall not be unreasonably withheld;"),
            ("(d)", "Pursue claims, conduct audits, and take such actions as Administrator deems "
             "reasonably necessary to recover unpaid or underpaid royalties owed in respect of "
             "the Compositions."),
        ]
        for label, text in grant_items:
            story.append(Paragraph(f"<b>{label}</b> {text}", indent_style))

        story.append(Paragraph(
            "1.2 Administrator shall have the right to appoint sub-publishers, licensees, or "
            "collection agents in foreign territories without requiring prior written consent "
            "from Writer, provided that: (a) any such appointment does not increase the total "
            "fee burden on Writer beyond the agreed Administration Fee set forth in Section 3; "
            "(b) Administrator shall remain fully liable for the acts and omissions of any "
            "sub-publisher, licensee, or collection agent so appointed; and (c) Administrator "
            "shall notify Writer in writing within thirty (30) days of any such appointment, "
            "identifying the appointed party and the territory or territories covered.",
            body_style,
        ))

        story.append(Paragraph(
            "1.3 For the avoidance of doubt, Writer shall retain ownership of all copyrights "
            "in and to the Compositions. This Agreement does not constitute an assignment or "
            "transfer of copyright ownership. Administrator's rights hereunder are limited to "
            "the administration rights expressly granted in this Section 1.",
            body_style,
        ))

        # Section 2: Term
        story.append(Paragraph("2. TERM", heading_style))
        story.append(Paragraph(f"2.1 {term_paragraph}", body_style))
        story.append(Paragraph(
            "2.2 Upon the expiration or termination of this Agreement, Administrator shall have "
            "a wind-down period not to exceed six (6) months to collect any royalties that accrued "
            "or became payable during the Term but were not yet received by Administrator as of the "
            "date of termination (the \"Collection Period\"). Administrator shall continue to account "
            "to Writer and remit Writer's share of such royalties during the Collection Period in "
            "accordance with Section 4.",
            body_style,
        ))

        # Section 3: Administration Fee
        story.append(Paragraph("3. ADMINISTRATION FEE", heading_style))
        story.append(Paragraph(
            "3.1 As compensation for the services rendered hereunder, Administrator shall be "
            "entitled to retain twenty percent (20%) of all gross royalties and income collected "
            'on behalf of Writer (the "Administration Fee").',
            body_style,
        ))
        story.append(Paragraph(
            "3.2 The remaining eighty percent (80%) of all gross royalties and income "
            'collected shall constitute Writer\'s share ("Writer\'s Share") and shall be '
            "payable to Writer in accordance with Section 4 hereof.",
            body_style,
        ))
        story.append(Paragraph(
            "3.3 The Administration Fee shall apply to all royalties collected during the Term "
            "and the Collection Period, regardless of when the underlying exploitation of the "
            "Compositions occurred.",
            body_style,
        ))

        # Section 4: Accounting and Payment
        story.append(Paragraph("4. ACCOUNTING AND PAYMENT", heading_style))
        story.append(Paragraph(
            "4.1 Administrator shall render written royalty statements to Writer on a monthly "
            "basis, within thirty (30) days following the end of each calendar month.",
            body_style,
        ))
        story.append(Paragraph(
            "4.2 Each statement shall set forth in reasonable detail the gross royalties collected, "
            "the Administration Fee deducted, and the net amount payable to Writer. Payment of "
            "Writer's Share shall accompany each monthly statement.",
            body_style,
        ))
        story.append(Paragraph(
            "4.3 All payments shall be made via electronic funds transfer (ACH/wire) to the bank "
            "account designated in writing by Writer, or by such other method as the Parties may "
            "mutually agree.",
            body_style,
        ))
        story.append(Paragraph(
            "4.4 Writer shall have the right, upon no less than thirty (30) days' prior written "
            "notice, to audit or cause to be audited Administrator's books and records relating "
            "to the Compositions, at Writer's sole expense, no more than once per calendar year. "
            "Such audit shall be conducted during normal business hours at Administrator's principal "
            "office. If an audit reveals an underpayment of five percent (5%) or more of the amounts "
            "due to Writer for any accounting period, Administrator shall bear the reasonable cost "
            "of such audit.",
            body_style,
        ))

        # Section 5: Representations and Warranties
        story.append(Paragraph("5. REPRESENTATIONS AND WARRANTIES", heading_style))
        story.append(Paragraph("5.1 Writer represents and warrants that:", body_style))
        warranty_items = [
            ("(a)", "Writer is the sole owner of, or otherwise controls, the Compositions and "
             "has the full right, power, and authority to enter into this Agreement and to grant "
             "the rights granted herein;"),
            ("(b)", "The Compositions are original works and do not infringe upon the copyrights, "
             "trademarks, or other intellectual property rights of any third party;"),
            ("(c)", "There are no pending or threatened claims, liens, encumbrances, or litigation "
             "relating to the Compositions that would materially impair Administrator's ability to "
             "perform its obligations hereunder;"),
            ("(d)", "Writer has not previously assigned, licensed, or encumbered the administration "
             "rights granted herein to any third party in a manner that would conflict with this "
             "Agreement."),
        ]
        for label, text in warranty_items:
            story.append(Paragraph(f"<b>{label}</b> {text}", indent_style))

        story.append(Paragraph(
            "5.2 Administrator represents and warrants that Administrator is a duly organized "
            "and validly existing entity "
            "and has the full right, power, and authority to enter into this Agreement "
            "and to perform its obligations hereunder.",
            body_style,
        ))

        # Section 6: Administrator Obligations
        story.append(Paragraph("6. ADMINISTRATOR'S OBLIGATIONS", heading_style))
        story.append(Paragraph(
            "6.1 Administrator shall use commercially reasonable efforts to:",
            body_style,
        ))
        obligation_items = [
            ("(a)", "Register the Compositions with all applicable PROs, collection societies, "
             "and digital licensing organizations;"),
            ("(b)", "Identify, claim, and recover royalties owed to Writer, including unclaimed "
             "or underpaid royalties from prior periods;"),
            ("(c)", "Monitor the exploitation of the Compositions and pursue claims for unauthorized "
             "use where commercially reasonable;"),
            ("(d)", "Provide transparent, accurate, and timely accounting of all income collected "
             "and fees deducted."),
        ]
        for label, text in obligation_items:
            story.append(Paragraph(f"<b>{label}</b> {text}", indent_style))

        # Section 7: Indemnification
        story.append(Paragraph("7. INDEMNIFICATION", heading_style))
        story.append(Paragraph(
            "7.1 Writer shall indemnify, defend, and hold harmless Administrator, its members, "
            "managers, officers, employees, and agents from and against any and all claims, "
            "demands, actions, damages, losses, liabilities, costs, and expenses (including "
            "reasonable attorneys' fees) arising out of or relating to any breach of Writer's "
            "representations, warranties, or obligations under this Agreement.",
            body_style,
        ))
        story.append(Paragraph(
            "7.2 Administrator shall indemnify, defend, and hold harmless Writer from and against "
            "any and all claims, demands, actions, damages, losses, liabilities, costs, and "
            "expenses (including reasonable attorneys' fees) arising out of or relating to any "
            "breach of Administrator's representations, warranties, or obligations under this "
            "Agreement, or any negligent or willful act or omission of Administrator in the "
            "performance of its duties hereunder.",
            body_style,
        ))

        # Section 8: Termination
        story.append(Paragraph("8. TERMINATION", heading_style))
        story.append(Paragraph(
            "8.1 Either Party may terminate this Agreement for cause upon thirty (30) days' "
            "written notice to the other Party if the other Party commits a material breach "
            "of any provision of this Agreement and fails to cure such breach within said "
            "thirty (30) day notice period.",
            body_style,
        ))
        story.append(Paragraph(
            "8.2 Upon any termination or expiration of this Agreement: (a) all administration "
            "rights granted to Administrator shall revert to Writer, subject to the Collection "
            "Period set forth in Section 2.2; (b) Administrator shall promptly deliver to Writer "
            "all documents, records, and materials in Administrator's possession relating to the "
            "Compositions; and (c) Administrator shall cooperate in good faith with Writer to "
            "transition administration to Writer or Writer's designee.",
            body_style,
        ))

        # Section 9: Limitation of Liability
        story.append(Paragraph("9. LIMITATION OF LIABILITY", heading_style))
        story.append(Paragraph(
            "IN NO EVENT SHALL EITHER PARTY BE LIABLE TO THE OTHER PARTY FOR ANY INDIRECT, "
            "INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES ARISING OUT OF OR RELATING "
            "TO THIS AGREEMENT, REGARDLESS OF THE THEORY OF LIABILITY, EXCEPT IN CASES OF WILLFUL "
            "MISCONDUCT OR GROSS NEGLIGENCE.",
            body_style,
        ))

        # Section 10: Confidentiality
        story.append(Paragraph("10. CONFIDENTIALITY", heading_style))
        story.append(Paragraph(
            "Each Party agrees to maintain the confidentiality of the other Party's proprietary "
            "and financial information disclosed in connection with this Agreement and shall not "
            "disclose such information to any third party without the prior written consent of "
            "the disclosing Party, except as required by law or as necessary to perform obligations "
            "under this Agreement.",
            body_style,
        ))

        # Section 11: General Provisions
        story.append(Paragraph("11. GENERAL PROVISIONS", heading_style))
        story.append(Paragraph(
            "<b>11.1 Governing Law.</b> This Agreement shall be governed by and construed in "
            "accordance with the laws of the State of California, without regard to its conflict "
            "of laws principles.",
            body_style,
        ))
        story.append(Paragraph(
            "<b>11.2 Dispute Resolution.</b> Any dispute, controversy, or claim arising out of "
            "or relating to this Agreement shall first be submitted to good-faith mediation in "
            "Los Angeles, California. If mediation fails to resolve the dispute within "
            "thirty (30) days, either Party may pursue binding arbitration administered by "
            "JAMS in Los Angeles, California, in accordance with its applicable rules "
            "and procedures. The arbitrator's decision shall be final and binding and may be "
            "entered as a judgment in any court of competent jurisdiction. Notwithstanding the "
            "foregoing, either Party may seek injunctive or other equitable relief in any court "
            "of competent jurisdiction.",
            body_style,
        ))
        story.append(Paragraph(
            "<b>11.3 Entire Agreement.</b> This Agreement constitutes the entire agreement "
            "between the Parties with respect to the subject matter hereof and supersedes all "
            "prior and contemporaneous negotiations, representations, understandings, and "
            "agreements, whether written or oral, relating to such subject matter.",
            body_style,
        ))
        story.append(Paragraph(
            "<b>11.4 Amendments.</b> This Agreement may not be amended, modified, or supplemented "
            "except by a written instrument signed by both Parties.",
            body_style,
        ))
        story.append(Paragraph(
            "<b>11.5 Assignment.</b> Neither Party may assign or transfer this Agreement or any "
            "rights or obligations hereunder without the prior written consent of the other Party, "
            "except that Administrator may assign this Agreement to a successor entity in connection "
            "with a merger, acquisition, or sale of substantially all of its assets, provided that "
            "the assignee assumes all obligations hereunder.",
            body_style,
        ))
        story.append(Paragraph(
            "<b>11.6 Notices.</b> All notices required or permitted under this Agreement shall be "
            "in writing and shall be deemed effectively given: (a) upon personal delivery; "
            "(b) upon transmission by email (with confirmation of receipt); or (c) one (1) "
            "business day after deposit with a nationally recognized overnight courier, addressed "
            "to the respective Party at the address set forth above or at such other address as a "
            "Party may designate by written notice.",
            body_style,
        ))
        story.append(Paragraph(
            "<b>11.7 Severability.</b> If any provision of this Agreement is held to be invalid, "
            "illegal, or unenforceable, the remaining provisions shall continue in full force and "
            "effect, and the invalid provision shall be modified to the minimum extent necessary "
            "to make it valid and enforceable.",
            body_style,
        ))
        story.append(Paragraph(
            "<b>11.8 Waiver.</b> The failure of either Party to enforce any provision of this "
            "Agreement shall not constitute a waiver of such Party's right to enforce such "
            "provision or any other provision in the future.",
            body_style,
        ))
        story.append(Paragraph(
            "<b>11.9 Counterparts.</b> This Agreement may be executed in counterparts, including "
            "by electronic signature, each of which shall be deemed an original and all of which "
            "together shall constitute one and the same instrument.",
            body_style,
        ))
        story.append(Paragraph(
            "<b>11.10 Independent Contractors.</b> The relationship between the Parties is that "
            "of independent contractors. Nothing in this Agreement shall be construed to create "
            "a partnership, joint venture, agency, or employment relationship between the Parties.",
            body_style,
        ))

        story.append(Spacer(1, 0.3 * inch))

        # Signature blocks with DocuSign anchor tags
        story.append(Paragraph(
            "<b>IN WITNESS WHEREOF</b>, the Parties have executed this Publishing Administration "
            "Agreement as of the date last signed below.",
            body_style,
        ))
        story.append(Spacer(1, 0.2 * inch))

        # Writer signature block
        story.append(Paragraph(f"<b>WRITER/PUBLISHER:</b>", bold_style))
        story.append(Paragraph(f"{legal_name} (p/k/a \"{producer_name}\")", body_style))
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph("Signature: /sn1/ ___________________________", sig_style))
        story.append(Paragraph("Date: /ds1/ _______________", sig_style))
        story.append(Spacer(1, 0.25 * inch))

        # Admin signature block
        story.append(Paragraph(f"<b>ADMINISTRATOR:</b>", bold_style))
        story.append(Paragraph(f"{self.ADMIN_COMPANY}", body_style))
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph("Signature: /sn2/ ___________________________", sig_style))
        story.append(Paragraph("Date: /ds2/ _______________", sig_style))

        # Term summary footer (small print for reference)
        story.append(Spacer(1, 0.3 * inch))
        small_style = ParagraphStyle("Small", parent=body_style, fontSize=7, textColor="#888888")
        story.append(Paragraph(f"Term: {term_summary} | Admin Fee: 20%", small_style))

        doc.build(story)
        buffer.seek(0)
        return buffer
