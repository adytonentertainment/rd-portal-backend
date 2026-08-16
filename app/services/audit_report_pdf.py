import io
from datetime import datetime

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


class AuditReportPDFGenerator:
    """Generates a catalog audit report PDF from free audit results."""

    def _vague_status(self, song: dict) -> str:
        """Map detailed issues to vague categories."""
        if not song.get("registered", False):
            return "Not Registered"
        issues = song.get("issues", [])
        if not issues:
            return "Registered"
        # Categorize without giving specifics
        lower = " ".join(issues).lower()
        if "ipi" in lower or "writer" in lower or "publisher" in lower:
            return "Matching Issue"
        if "isrc" in lower or "metadata" in lower:
            return "Metadata Issue"
        return "Issue Detected"

    def generate(self, audit_data: dict) -> io.BytesIO:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            topMargin=50,
            bottomMargin=50,
            leftMargin=50,
            rightMargin=50,
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "ReportTitle",
            parent=styles["Title"],
            fontSize=18,
            spaceAfter=4,
            fontName="Helvetica-Bold",
            textColor=HexColor("#111111"),
        )
        subtitle_style = ParagraphStyle(
            "Subtitle",
            parent=styles["Normal"],
            fontSize=10,
            spaceAfter=20,
            textColor=HexColor("#666666"),
        )
        heading_style = ParagraphStyle(
            "SectionHeading",
            parent=styles["Heading2"],
            fontSize=12,
            spaceBefore=16,
            spaceAfter=8,
            fontName="Helvetica-Bold",
            textColor=HexColor("#111111"),
        )
        body_style = ParagraphStyle(
            "Body",
            parent=styles["Normal"],
            fontSize=9,
            leading=13,
            spaceAfter=4,
        )
        cell_style = ParagraphStyle(
            "Cell",
            parent=styles["Normal"],
            fontSize=8,
            leading=10,
        )
        cell_bold = ParagraphStyle(
            "CellBold",
            parent=cell_style,
            fontName="Helvetica-Bold",
        )
        cta_style = ParagraphStyle(
            "CTA",
            parent=styles["Normal"],
            fontSize=10,
            leading=14,
            textColor=HexColor("#6366f1"),
            alignment=1,
            spaceBefore=12,
        )
        footer_style = ParagraphStyle(
            "Footer",
            parent=styles["Normal"],
            fontSize=7,
            textColor=HexColor("#999999"),
            alignment=1,
        )

        artist_name = audit_data.get("artistName", "Unknown Artist")
        summary = audit_data.get("summary", {})
        songs = audit_data.get("songs", [])
        date_str = datetime.now().strftime("%B %d, %Y")

        total = summary.get("total", len(songs))
        registered = summary.get("registered", 0)
        unregistered = summary.get("unregistered", 0)
        issues = summary.get("issueCount", 0)

        story = []

        # Header
        story.append(Paragraph("Catalog Audit Report", title_style))
        story.append(Paragraph(f"{artist_name} — {date_str}", subtitle_style))

        # Summary
        story.append(Paragraph("Summary", heading_style))
        summary_data = [
            ["Total Songs", "Registered", "Not Registered", "Issues Found"],
            [str(total), str(registered), str(unregistered), str(issues)],
        ]
        summary_table = Table(summary_data, colWidths=[125, 125, 125, 125])
        summary_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), HexColor("#f5f5f5")),
            ("TEXTCOLOR", (0, 0), (-1, 0), HexColor("#333333")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#dddddd")),
            ("TEXTCOLOR", (1, 1), (1, 1), HexColor("#22c55e")),
            ("TEXTCOLOR", (2, 1), (2, 1), HexColor("#ef4444")),
            ("TEXTCOLOR", (3, 1), (3, 1), HexColor("#f59e0b")),
            ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 1), (-1, 1), 14),
        ]))
        story.append(summary_table)
        story.append(Spacer(1, 0.2 * inch))

        # Songs table — vague statuses, no ISRCs, no issue details
        if songs:
            story.append(Paragraph("Songs", heading_style))

            header = [
                Paragraph("<b>#</b>", cell_bold),
                Paragraph("<b>Title</b>", cell_bold),
                Paragraph("<b>Artist</b>", cell_bold),
                Paragraph("<b>Status</b>", cell_bold),
            ]
            rows = [header]

            for i, song in enumerate(songs, 1):
                status = self._vague_status(song)
                rows.append([
                    Paragraph(str(i), cell_style),
                    Paragraph(song.get("title", "")[:45], cell_style),
                    Paragraph(song.get("artist", "")[:30], cell_style),
                    Paragraph(status, cell_bold),
                ])

            col_widths = [30, 210, 140, 130]
            table = Table(rows, colWidths=col_widths, repeatRows=1)

            table_styles = [
                ("BACKGROUND", (0, 0), (-1, 0), HexColor("#111111")),
                ("TEXTCOLOR", (0, 0), (-1, 0), HexColor("#ffffff")),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#dddddd")),
            ]

            for idx in range(1, len(rows)):
                if idx % 2 == 0:
                    table_styles.append(
                        ("BACKGROUND", (0, idx), (-1, idx), HexColor("#fafafa"))
                    )
                song_data = songs[idx - 1] if idx - 1 < len(songs) else {}
                if not song_data.get("registered", False):
                    table_styles.append(
                        ("TEXTCOLOR", (3, idx), (3, idx), HexColor("#ef4444"))
                    )
                elif song_data.get("issues"):
                    table_styles.append(
                        ("TEXTCOLOR", (3, idx), (3, idx), HexColor("#f59e0b"))
                    )
                else:
                    table_styles.append(
                        ("TEXTCOLOR", (3, idx), (3, idx), HexColor("#22c55e"))
                    )

            table.setStyle(TableStyle(table_styles))
            story.append(table)

        # CTA
        story.append(Spacer(1, 0.3 * inch))
        story.append(Paragraph(
            "Sign up for free at <b>verax.app</b> for full details and resolution.",
            cta_style,
        ))

        # Footer
        story.append(Spacer(1, 0.3 * inch))
        story.append(Paragraph(
            "Powered by Verax — verax.app",
            footer_style,
        ))

        doc.build(story)
        buffer.seek(0)
        return buffer
