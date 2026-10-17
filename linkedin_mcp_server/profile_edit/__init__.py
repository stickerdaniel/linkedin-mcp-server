"""Human-approved editing of the authenticated member's own LinkedIn profile.

Every write follows READ -> PROPOSE -> PREVIEW -> explicit APPLY -> VERIFY.
Everything in this package except ``tools`` wiring is browser-free: it talks to
LinkedIn only through the ``ProfileEditorPort`` protocol, which the page-owning
``linkedin.profile_editor.ProfileEditor`` implements.
"""
