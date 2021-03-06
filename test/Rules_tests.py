import os

import unittest

import Params
import Runtime
import Rules



class Rules_Join(unittest.TestCase):

	test_rules = 'etc/rules.join'

	def test_1_1_defaults(self):
		self.assertEqual(
			Params.JOIN_FILE, '/etc/htcache/rules.join')

	def test_1_2_init(self):
		self.assertEqual(Rules.Join.rules, [])
		fn = 'etc/rules.join'
		Rules.Join.parse(fn) 
		self.assertEqual(len(Rules.Join.rules), 2)

	def test_2_rewrite(self):
		path = 'tools.ietf.org/html/rfc4716'
		self.assertEqual(len(Rules.Join.rules), 2)
		path_new = Rules.Join.rewrite(path)
		self.assertEqual('media/site/tools.ietf.org/html/rfc4716', path_new)

	def test_3_validate(self):
		self.assertEqual(len(Rules.Join.rules), 2)
		self.assertTrue(
				Rules.Join.validate())


class Rules_Drop(unittest.TestCase):
	pass

class Rules_Rewrite(unittest.TestCase):
	pass


