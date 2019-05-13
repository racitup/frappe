# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt

from __future__ import unicode_literals, absolute_import
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import validate_email_address, get_fullname, strip_html, cstr
from frappe.core.doctype.communication.email import (validate_email,
	notify, _notify, update_parent_mins_to_first_response)
from frappe.core.utils import get_parent_doc
from frappe.utils.bot import BotReply
from frappe.utils import parse_addr
from frappe.core.doctype.comment.comment import update_comment_in_doc

from collections import Counter

exclude_from_linked_with = True

class Communication(Document):
	no_feed_on_delete = True

	"""Communication represents an external communication like Email."""
	def onload(self):
		"""create email flag queue"""
		if self.communication_type == "Communication" and self.communication_medium == "Email" \
			and self.sent_or_received == "Received" and self.uid and self.uid != -1:

			email_flag_queue = frappe.db.get_value("Email Flag Queue", {
				"communication": self.name,
				"is_completed": 0})
			if email_flag_queue:
				return

			frappe.get_doc({
				"doctype": "Email Flag Queue",
				"action": "Read",
				"communication": self.name,
				"uid": self.uid,
				"email_account": self.email_account
			}).insert(ignore_permissions=True)
			frappe.db.commit()

	def validate(self):
		self.validate_reference()

		if not self.user:
			self.user = frappe.session.user

		if not self.subject:
			self.subject = strip_html((self.content or "")[:141])

		if not self.sent_or_received:
			self.seen = 1
			self.sent_or_received = "Sent"

		self.set_status()
		self.set_sender_full_name()

		validate_email(self)

	def validate_reference(self):
		if self.reference_doctype and self.reference_name:
			if not self.reference_owner:
				self.reference_owner = frappe.db.get_value(self.reference_doctype, self.reference_name, "owner")

			# prevent communication against a child table
			if frappe.get_meta(self.reference_doctype).istable:
				frappe.throw(_("Cannot create a {0} against a child document: {1}")
					.format(_(self.communication_type), _(self.reference_doctype)))

			# Prevent circular linking of Communication DocTypes
			if self.reference_doctype == "Communication":
				circular_linking = False
				doc = get_parent_doc(self)
				while doc.reference_doctype == "Communication":
					if get_parent_doc(doc).name==self.name:
						circular_linking = True
						break
					doc = get_parent_doc(doc)
				if circular_linking:
					frappe.throw(_("Please make sure the Reference Communication Docs are not circularly linked."), frappe.CircularLinkingError)

	def after_insert(self):
		if not (self.reference_doctype and self.reference_name):
			return

		if self.reference_doctype == "Communication" and self.sent_or_received == "Sent":
			frappe.db.set_value("Communication", self.reference_name, "status", "Replied")

		if self.communication_type == "Communication":
			# send new comment to listening clients
			frappe.publish_realtime('new_communication', self.as_dict(),
				doctype=self.reference_doctype, docname=self.reference_name,
				after_commit=True)

		elif self.communication_type in ("Chat", "Notification", "Bot"):
			if self.reference_name == frappe.session.user:
				message = self.as_dict()
				message['broadcast'] = True
				frappe.publish_realtime('new_message', message, after_commit=True)
			else:
				# reference_name contains the user who is addressed in the messages' page comment
				frappe.publish_realtime('new_message', self.as_dict(),
					user=self.reference_name, after_commit=True)

	def on_update(self):
		# add to _comment property of the doctype, so it shows up in
		# comments count for the list view
		update_comment_in_doc(self)

		if self.comment_type != 'Updated':
			update_parent_mins_to_first_response(self)
			self.bot_reply()

	def on_trash(self):
		if self.communication_type == "Communication":
			# send delete comment to listening clients
			frappe.publish_realtime('delete_communication', self.as_dict(),
				doctype= self.reference_doctype, docname = self.reference_name,
				after_commit=True)

	def set_status(self):
		if not self.is_new():
			return

		if self.reference_doctype and self.reference_name:
			self.status = "Linked"
		elif self.communication_type=="Communication":
			self.status = "Open"
		else:
			self.status = "Closed"

		# set email status to spam
		email_rule = frappe.db.get_value("Email Rule", { "email_id": self.sender, "is_spam":1 })
		if self.communication_type == "Communication" and self.communication_medium == "Email" \
			and self.sent_or_received == "Sent" and email_rule:

			self.email_status = "Spam"

	def set_sender_full_name(self):
		if not self.sender_full_name and self.sender:
			if self.sender == "Administrator":
				self.sender_full_name = frappe.db.get_value("User", "Administrator", "full_name")
				self.sender = frappe.db.get_value("User", "Administrator", "email")
			elif self.sender == "Guest":
				self.sender_full_name = self.sender
				self.sender = None
			else:
				if self.sent_or_received=='Sent':
					validate_email_address(self.sender, throw=True)
				sender_name, sender_email = parse_addr(self.sender)
				if sender_name == sender_email:
					sender_name = None
				self.sender = sender_email
				self.sender_full_name = sender_name or get_fullname(frappe.session.user) if frappe.session.user!='Administrator' else None

	def send(self, print_html=None, print_format=None, attachments=None,
		send_me_a_copy=False, recipients=None):
		"""Send communication via Email.

		:param print_html: Send given value as HTML attachment.
		:param print_format: Attach print format of parent document."""

		self.send_me_a_copy = send_me_a_copy
		self.notify(print_html, print_format, attachments, recipients)

	def notify(self, print_html=None, print_format=None, attachments=None,
		recipients=None, cc=None, bcc=None,fetched_from_email_account=False):
		"""Calls a delayed task 'sendmail' that enqueus email in Email Queue queue

		:param print_html: Send given value as HTML attachment
		:param print_format: Attach print format of parent document
		:param attachments: A list of filenames that should be attached when sending this email
		:param recipients: Email recipients
		:param cc: Send email as CC to
		:param fetched_from_email_account: True when pulling email, the notification shouldn't go to the main recipient

		"""
		notify(self, print_html, print_format, attachments, recipients, cc, bcc,
			fetched_from_email_account)

	def _notify(self, print_html=None, print_format=None, attachments=None,
		recipients=None, cc=None, bcc=None):

		_notify(self, print_html, print_format, attachments, recipients, cc, bcc)

	def bot_reply(self):
		if self.comment_type == 'Bot' and self.communication_type == 'Chat':
			reply = BotReply().get_reply(self.content)
			if reply:
				frappe.get_doc({
					"doctype": "Communication",
					"comment_type": "Bot",
					"communication_type": "Bot",
					"content": cstr(reply),
					"reference_doctype": self.reference_doctype,
					"reference_name": self.reference_name
				}).insert()
				frappe.local.flags.commit = True

	def set_delivery_status(self, commit=False):
		'''Look into the status of Email Queue linked to this Communication and set the Delivery Status of this Communication'''
		delivery_status = None
		status_counts = Counter(frappe.db.sql_list('''select status from `tabEmail Queue` where communication=%s''', self.name))
		if self.sent_or_received == "Received":
			return

		if status_counts.get('Not Sent') or status_counts.get('Sending'):
			delivery_status = 'Sending'

		elif status_counts.get('Error'):
			delivery_status = 'Error'

		elif status_counts.get('Expired'):
			delivery_status = 'Expired'

		elif status_counts.get('Sent'):
			delivery_status = 'Sent'

		if delivery_status:
			self.db_set('delivery_status', delivery_status)

			frappe.publish_realtime('update_communication', self.as_dict(),
				doctype=self.reference_doctype, docname=self.reference_name, after_commit=True)

			# for list views and forms
			self.notify_update()

			if commit:
				frappe.db.commit()

	# Timeline Links
	def deduplicate_dynamic_links(self):
		if self.dynamic_links:
			links, duplicate = [], False

			for l in self.dynamic_links:
				t = (l.link_doctype, l.link_name)
				if not t in links:
					links.append(t)
				else:
					duplicate = True

			if duplicate:
				del self.dynamic_links[:] # make it python 2 compatible as list.clear() is python 3 only
				for l in links:
					self.add_link(link_doctype=l[0], link_name=l[1])

	def validate_circular_links(self):
		for dynamic_link in self.dynamic_links:
			# Prevent circular linking of Communication DocTypes
			if dynamic_link.link_doctype == "Communication":
				circular_linking = False
				circular_level_1 = get_timeline_parent_doc(dynamic_link.link_doctype, dynamic_link.link_name)

				# Level 1
				if circular_level_1:
					for link in circular_level_1.dynamic_links:
						if link.link_doctype == "Communication":
							circular_level_2 = get_timeline_parent_doc(link.link_doctype, link.link_name)

							# Level 2
							if circular_level_2:
								for ref_link in circular_level_2.dynamic_links:
									if ref_link.link_doctype == "Communication":
										circular_level_3 = get_timeline_parent_doc(ref_link.link_doctype, ref_link.link_name)

										# Level 3
										if circular_level_3:
											if circular_level_3.name == self.name:
												circular_linking = True
												break
						if circular_linking:
							frappe.throw(_("Please make sure the Timeline Communication Docs are not circularly linked."), frappe.CircularLinkingError)

	def add_link(self, link_doctype, link_name, autosave=False):
		self.append("dynamic_links",
			{
				"link_doctype": link_doctype,
				"link_name": link_name
			}
		)

		if autosave:
			self.save(ignore_permissions=True)

	def get_links(self):
		return self.dynamic_links

	def remove_link(self, link_doctype, link_name, autosave=False, ignore_permissions=True):
		for l in self.dynamic_links:
			if l.link_doctype == link_doctype and l.link_name == link_name:
				self.dynamic_links.remove(l)

		if autosave:
			self.save(ignore_permissions=ignore_permissions)

def on_doctype_update():
	"""Add indexes in `tabCommunication`"""
	frappe.db.add_index("Communication", ["reference_doctype", "reference_name"])
	frappe.db.add_index("Communication", ["link_doctype", "link_name"])
	frappe.db.add_index("Communication", ["status", "communication_type"])

def has_permission(doc, ptype, user):
	if ptype=="read":
		if doc.reference_doctype == "Communication" and doc.reference_name == doc.name:
			return

		if doc.reference_doctype and doc.reference_name:
			if frappe.has_permission(doc.reference_doctype, ptype="read", doc=doc.reference_name):
				return True

def get_permission_query_conditions_for_communication(user):
	if not user: user = frappe.session.user

	roles = frappe.get_roles(user)

	if "Super Email User" in roles or "System Manager" in roles:
		return None
	else:
		accounts = frappe.get_all("User Email", filters={ "parent": user },
			fields=["email_account"],
			distinct=True, order_by="idx")

		if not accounts:
			return """tabCommunication.communication_medium!='Email'"""

		email_accounts = [ '"%s"'%account.get("email_account") for account in accounts ]
		return """tabCommunication.email_account in ({email_accounts})"""\
			.format(email_accounts=','.join(email_accounts))

def get_timeline_parent_doc(link_doctype, link_name):
	"""Returns document of `link_doctype`, `link_name`"""
	if link_doctype and link_name:
		parent_doc = frappe.get_doc(link_doctype, link_name)
	return parent_doc if parent_doc else None
