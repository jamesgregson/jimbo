---
name: grill-me
description: Interview the user about a plan or design until you both understand it the same way. Use when the user asks to be grilled or to stress-test a plan
---
Interview the user about every part of their plan until you both understand it the same way.

- Treat the plan as a tree of decisions. Follow each branch to the end before you start the next one. Settle
  each decision before the decisions that depend on it.
- Ask one question at a time. Use `ask_user` if you have it: make the question `yes_no` or a `choice`, with
  your recommended answer first. Without `ask_user`, ask in a message and give your recommended answer. If a
  question has no short list of answers, such as "which settings should be editable?", list the likely
  answers as options of a `multiple` choice, or ask it in a message.
- Before you ask a question, check if the code answers it. If it does, read the code instead of asking.
- Challenge answers that conflict with earlier answers, with the code, or with the stated goal. Say what
  conflicts and ask which one should change.
- If the user stops a question to say something else, follow what they said. Don't go back to the old question
  unless it still matters.

Stop when every branch is settled. Then list the decisions you agreed on, one line each, and any open questions.
