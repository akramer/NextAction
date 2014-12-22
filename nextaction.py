#!/usr/bin/env python

import argparse
import copy
import dateutil.parser
import dateutil.tz
import datetime
import json
import logging
import time
import urllib
import urllib2

NEXT_ACTION_LABEL = u'next_action'
args = None

class TraversalState(object):
  """Simple class to contain the state of the item tree traversal."""
  def __init__(self, next_action_label_id):
    self.remove_labels = []
    self.add_labels = []
    self.found_next_action = False
    self.next_action_label_id = next_action_label_id

  def clone(self):
    """Perform a simple clone of this state object.

    For parallel traversals it's necessary to produce copies so that every
    traversal to a lower node has the same found_next_action status.
    """
    t = TraversalState(self.next_action_label_id)
    t.found_next_action = self.found_next_action
    return t

  def merge(self, other):
    """Merge clones back together.

    After parallel traversals, merge the results back into the parent state.
    """
    if other.found_next_action:
      self.found_next_action = True
    self.remove_labels += other.remove_labels
    self.add_labels += other.add_labels


class Item(object):
  def __init__(self, initial_data):
    self.parent = None
    self.children = []
    self.checked = initial_data['checked'] == 1
    self.content = initial_data['content']
    self.indent = initial_data['indent']
    self.item_id = initial_data['id']
    self.labels = initial_data['labels']
    self.priority = initial_data['priority']
    if 'due_date_utc' in initial_data and initial_data['due_date_utc'] != None:
      p = dateutil.parser.parser()
      self.due_date_utc = p.parse(initial_data['due_date_utc'])
    else:
      # Arbitrary time in the future to always sort last
      self.due_date_utc = datetime.datetime(2100, 1, 1, tzinfo=dateutil.tz.tzutc())

  def GetItemMods(self, state):
    if self.IsSequential():
      self._SequentialItemMods(state)
    elif self.IsParallel():
      self._ParallelItemMods(state)
    if not state.found_next_action and not self.checked:
      state.found_next_action = True
      if args.use_priority and self.priority != 4:
        state.add_labels.append(self)
      elif not args.use_priority and not state.next_action_label_id in self.labels:
        state.add_labels.append(self)
    else:
      if args.use_priority and self.priority == 4:
        state.remove_labels.append(self)
      elif not args.use_priority and state.next_action_label_id in self.labels:
        state.remove_labels.append(self)

  def SortChildren(self):
    # Sorting by priority and date seemed like a good idea at some point, but
    # that has proven wrong. Don't sort.
    pass

  def GetLabelRemovalMods(self, state):
    if args.use_priority:
      return
    if state.next_action_label_id in self.labels:
      state.remove_labels.append(self)
    for item in self.children:
      item.GetLabelRemovalMods(state)

  def _SequentialItemMods(self, state):
    """
    Iterate over every child, walking down the tree.
    If none of our children are the next action, check if we are.
    """
    for item in self.children:
      item.GetItemMods(state)

  def _ParallelItemMods(self, state):
    """
    Iterate over every child, walking down the tree.
    If none of our children are the next action, check if we are.
    Clone the state each time we descend down to a child.
    """
    frozen_state = state.clone()
    for item in self.children:
      temp_state = frozen_state.clone()
      item.GetItemMods(temp_state)
      state.merge(temp_state)

  def IsSequential(self):
    return not self.content.endswith('=')

  def IsParallel(self):
    return self.content.endswith('=')

class Project(object):
  def __init__(self, initial_data):
    self.unsorted_items = dict()
    self.children = []
    self.indent = 0
    self.is_archived = initial_data['is_archived'] == 1
    self.is_deleted = initial_data['is_deleted'] == 1
    self.last_updated = initial_data['last_updated']
    self.name = initial_data['name']
    # Project should act like an item, so it should have content.
    self.content = initial_data['name']
    self.project_id = initial_data['id']

  def UpdateChangedData(self, changed_data):
    self.name = changed_data['name']
    self.last_updated = changed_data['last_updated']

  def IsSequential(self):
    return self.name.endswith('--')

  def IsParallel(self):
    return self.name.endswith('=')

  SortChildren = Item.__dict__['SortChildren']

  def GetItemMods(self, state):
    if self.IsSequential():
      for item in self.children:
        item.GetItemMods(state)
    elif self.IsParallel():
      frozen_state = state.clone()
      for item in self.children:
        temp_state = frozen_state.clone()
        item.GetItemMods(temp_state)
        state.merge(temp_state)
    else: # Remove all next_action labels in this project.
      for item in self.children:
        item.GetLabelRemovalMods(state)

  def AddItem(self, item):
    '''Collect unsorted child items

    All child items for all projects are bundled up into an 'Items' list in the
    v5 api. They must be normalized and then sorted to make use of them.'''
    self.unsorted_items[item['id']] = item

  def DelItem(self, item):
    '''Delete unsorted child items'''
    del self.unsorted_items[item['id']]


  def BuildItemTree(self):
    '''Build a tree of items build off the unsorted list

    Sort the unsorted children first so that indentation levels mean something.
    '''
    self.children = []
    sortfunc = lambda item: item['item_order']
    sorted_items = sorted(self.unsorted_items.values(), key=sortfunc)
    parent_item = self
    previous_item = self
    for item_dict in sorted_items:
      item = Item(item_dict)
      if item.indent > previous_item.indent:
        logging.debug('pushing "%s" on the parent stack beneath "%s"',
            previous_item.content, parent_item.content)
        parent_item = previous_item
      # walk up the tree until we reach our parent
      while item.indent <= parent_item.indent:
        logging.debug('walking up the tree from "%s" to "%s"',
            parent_item.content, parent_item.parent.content)
        parent_item = parent_item.parent

      logging.debug('adding item "%s" with parent "%s"', item.content,
          parent_item.content)
      parent_item.children.append(item)
      item.parent = parent_item
      previous_item = item
    #self.SortChildren()


class TodoistData(object):
  '''Construct an object based on a full Todoist /Get request's data'''
  def __init__(self, initial_data):

    self._next_action_id = None
    self._SetLabelData(initial_data)
    self._projects = dict()
    self._seq_no = initial_data['seq_no']
    for project in initial_data['Projects']:
      if project['is_deleted'] == 0:
        self._projects[project['id']] = Project(project)
    for item in initial_data['Items']:
      self._projects[item['project_id']].AddItem(item)
    for project in self._projects.itervalues():
      project.BuildItemTree()

  def _SetLabelData(self, label_data):
    if args.use_priority:
      return
    if 'Labels' not in label_data:
      logging.debug("Label data not found, wasn't updated.")
      return
    # Store label data - we need this to set the next_action label.
    for label in label_data['Labels']:
      if label['name'] == NEXT_ACTION_LABEL:
        self._next_action_id = label['id']
        logging.info('Found next_action label, id: %s', label['id'])
    if self._next_action_id == None:
        logging.warning('Failed to find next_action label, need to create it.')

  def GetSyncState(self):
    return {'seq_no': self._seq_no}

  def UpdateChangedData(self, changed_data):
    if 'seq_no' in changed_data:
      self._seq_no = changed_data['seq_no']
    if 'TempIdMapping' in changed_data:
      if self._next_action_id in changed_data['TempIdMapping']:
        logging.info('Discovered temp->real next_action mapping ID')
        self._next_action_id = changed_data['TempIdMapping'][self._next_action_id]
    if 'Projects' in changed_data:
      for project in changed_data['Projects']:
	# delete missing projects
        if project['is_deleted'] == 1:
          logging.info('forgetting deleted project %s' % project['name'])
          del self._projects[project['project_id']]
        if project['id'] in self._projects:
          self._projects[project['id']].UpdateChangedData(project)
        else :
          logging.info('found new project: %s' % project['name'])
          self._projects[project['id']] = Project(project)
    if 'Items' in changed_data:
      for item in changed_data['Items']:
        if item['is_deleted'] == 1:
          logging.info('removing deleted item %d from project %d' % (item['id'], item['project_id']))
          self._projects[item['project_id']].DelItem(item)
        else:
          self._projects[item['project_id']].AddItem(item)
    for project in self._projects.itervalues():
      project.BuildItemTree()
    
  def GetProjectMods(self):
    mods = []
    # We need to create the next_action label
    if self._next_action_id == None and not args.use_priority:
      self._next_action_id = '$%d' % int(time.time())
      mods.append({'type': 'label_register',
                   'timestamp': int(time.time()),
                   'temp_id': self._next_action_id,
                   'args': {
                     'name': NEXT_ACTION_LABEL
                    }})
      # Exit early so that we can receive the real ID for the label.
      # Otherwise we end up applying the label two different times, once with
      # the temporary ID and once with the real one.
      # This makes adding the label take an extra round through the sync
      # process, but that's fine since this only happens on the first ever run.
      logging.info("Adding next_action label")
      return mods
    for project in self._projects.itervalues():
      state = TraversalState(self._next_action_id)
      project.GetItemMods(state)
      if len(state.add_labels) > 0 or len(state.remove_labels) > 0:
        logging.info("For project %s, the following mods:", project.name)
      for item in state.add_labels:
        # Intentionally add the next_action label to the item.
        # This prevents us from applying the label twice since the sync
        # interface does not return our changes back to us on GetAndSync.
        # Apply these changes to both the item in the tree and the unsorted
        # data.
        # I really don't like this aspect of the API - return me a full copy of
        # changed items please.
        #
        # Also... OMFG. "Priority 1" in the UI is actually priority 4 via the API.
        # Lowest priority = 1 here, but that is the word for highest priority
        # on the website.
        m = self.MakeNewMod(item)
        mods.append(m)
        if args.use_priority:
          item.priority = 4
          project.unsorted_items[item.item_id]['priority'] = 4
          m['args']['priority'] = item.priority
        else:
          item.labels.append(self._next_action_id)
          m['args']['labels'] = item.labels
        logging.info("add next_action to: %s", item.content)
      for item in state.remove_labels:
        m = self.MakeNewMod(item)
        mods.append(m)
        if args.use_priority:
          item.priority = 1
          project.unsorted_items[item.item_id]['priority'] = 1
          m['args']['priority'] = item.priority
        else:
          item.labels.remove(self._next_action_id)
          m['args']['labels'] = item.labels
        logging.info("remove next_action from: %s", item.content)
    return mods

  @staticmethod
  def MakeNewMod(item):
    return {'type': 'item_update',
            'timestamp': int(time.time()),
            'args': {
              'id': item.item_id,
              }
            }




def GetResponse(api_token):
  values = {'api_token': api_token, 'seq_no': '0'}
  data = urllib.urlencode(values)
  req = urllib2.Request('https://api.todoist.com/TodoistSync/v5.3/get', data)
  return urllib2.urlopen(req)

def DoSyncAndGetUpdated(api_token, items_to_sync, sync_state):
  values = {'api_token': api_token,
            'items_to_sync': json.dumps(items_to_sync)}
  for key, value in sync_state.iteritems():
    values[key] = json.dumps(value)
  logging.debug("posting %s", values)
  data = urllib.urlencode(values)
  req = urllib2.Request('https://api.todoist.com/TodoistSync/v5.3/syncAndGetUpdated', data)
  return urllib2.urlopen(req)

def main():
  parser = argparse.ArgumentParser(description='Add NextAction labels to Todoist.')
  parser.add_argument('--api_token', required=True, help='Your API key')
  parser.add_argument('--use_priority', required=False,
      action="store_true", help='Use priority 1 rather than a label to indicate the next actions.')
  global args
  args = parser.parse_args()
  logging.basicConfig(level=logging.DEBUG)
  response = GetResponse(args.api_token)
  initial_data = response.read()
  logging.debug("Got initial data: %s", initial_data)
  initial_data = json.loads(initial_data)
  a = TodoistData(initial_data)
  while True:
    mods = a.GetProjectMods()
    if len(mods) == 0:
      time.sleep(5)
    else:
      logging.info("* Modifications necessary - skipping sleep cycle.")
    logging.info("** Beginning sync")
    sync_state = a.GetSyncState()
    changed_data = DoSyncAndGetUpdated(args.api_token,mods, sync_state).read()
    logging.debug("Got sync data %s", changed_data)
    changed_data = json.loads(changed_data)
    logging.info("* Updating model after receiving sync data")
    a.UpdateChangedData(changed_data)
    logging.info("* Finished updating model")
    logging.info("** Finished sync")

if __name__ == '__main__':
  main()
