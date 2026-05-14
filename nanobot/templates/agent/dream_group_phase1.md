You are extracting shared facts from a GROUP CONVERSATION for SHARED.md only.

CRITICAL PRIVACY BOUNDARY:
- This is a GROUP chat (3+ members), NOT a private DM
- ONLY extract facts suitable for SHARED.md (family info, shared preferences, group context)
- NEVER extract personal/private information (health, finances, dating, solo trips)
- If uncertain whether something is shared vs private, DO NOT extract it

Output one line per finding:
[SHARED] atomic fact about shared/family/group information
[SHARED-REMOVE] reason to remove outdated shared info

What belongs in SHARED.md:
- Family members, relationships, birthdays, anniversaries
- Shared dietary restrictions or allergies
- Home location, shared travel plans
- Group preferences (restaurants, activities the group enjoys)
- Shared contacts, mutual friends
- Group decisions or agreements

What does NOT belong (never extract from group chats):
- Individual health information
- Personal finances or budgets
- Dating or personal relationships
- Solo trip plans
- Work details of individual members
- Anything one person wouldn't want others in the group to know

Rules:
- Atomic facts: "daughter Emma's birthday is October 1" not "discussed family"
- Must be confirmed information, not speculation
- If the fact could embarrass someone if shared, skip it

[SKIP] if nothing suitable for SHARED.md extraction.
