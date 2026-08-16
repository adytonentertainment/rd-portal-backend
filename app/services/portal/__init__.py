"""Writer-portal service layer: access sharing (invites) and contact scoping.

The portal's access model is Dropbox-style (infra PRD §7.2): a writer is the
shared "folder", a WriterContact link is membership, and a PortalInvite is a
pending share. Anyone with access to a writer can invite another email; the
admin sends the first invite to bootstrap each writer.
"""
