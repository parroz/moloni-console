"""Hava — the Telegram agent that turns supplier invoices into Moloni drafts.

Runs as its own process (`python -m app.hava`) next to the console API and
reuses its extraction, apply and Moloni code. Talks Portuguese, lives in one
Telegram group, and asks the group whenever an invoice leaves it in doubt.
"""
