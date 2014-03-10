#!/usr/bin/env python

import copy
import dateutil.parser
import dateutil.tz
import datetime
import json
import logging
import time
import urllib
import urllib2

API_TOKEN = 'YOUR_API_TOKEN_HERE'
NEXT_ACTION_LABEL = u'next_action'

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
      if not state.next_action_label_id in self.labels:
        state.add_labels.append(self)
    elif state.next_action_label_id in self.labels:
      state.remove_labels.append(self)

  def SortChildren(self):
    sortfunc = lambda item: [item.due_date_utc, (5 - item.priority)]
    self.children = sorted(self.children, key=sortfunc)
    for item in self.children:
      item.SortChildren()

  def GetLabelRemovalMods(self, state):
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
    #if self.content.endswith('--') or self.content.endswith('='):
    #  return self.content.endswith('--')
    #else:
    #  return self.parent.IsSequential()

  def IsParallel(self):
    return self.content.endswith('=')
    #if self.content.endswith('--') or self.content.endswith('='):
    #  return self.content.endswith('=')
    #else:
    #  return self.parent.IsParallel()

class Project(object):
  def __init__(self, initial_data):
    self.children = []
    self.indent = 0
    self.is_archived = initial_data['is_archived'] == 1
    self.is_deleted = initial_data['is_deleted'] == 1
    self.last_updated = initial_data['last_updated']
    self.name = initial_data['name']
    # Project should act like an item, so it should have content.
    self.content = initial_data['name']
    self.project_id = initial_data['id']
    self._CreateItemTree(initial_data['items'])
    self.SortChildren()

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

  def _CreateItemTree(self, items):
    '''Build a tree of items based on their indentation level.'''
    parent_item = self
    previous_item = self
    for item_dict in items:
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


class TodoistData(object):
  '''Construct an object based on a full Todoist /Get request's data'''
  def __init__(self, initial_data):

    self._SetLabelData(initial_data)
    self._projects = dict()
    for project in initial_data['Projects']:
      self._projects[project['id']] = Project(project)

  def _SetLabelData(self, label_data):
    # Store label data - we need this to set the next_action label.
    self._labels_timestamp = label_data['LabelsTimestamp']
    self._next_action_id = None
    for label in label_data['Labels']:
      if label['name'] == NEXT_ACTION_LABEL:
        self._next_action_id = label['id']
        logging.info('Found next_action label, id: %s', label['id'])
    if self._next_action_id == None:
        logging.warning('Failed to find next_action label, need to create it.')

  def GetSyncState(self):
    project_timestamps = dict()
    for project_id, project in self._projects.iteritems():
      project_timestamps[project_id] = project.last_updated
    return {'labels_timestamp': self._labels_timestamp,
            'project_timestamps': project_timestamps}

  def UpdateChangedData(self, changed_data):
    if ('LabelsTimestamp' in changed_data
        and changed_data['LabelsTimestamp'] != self._labels_timestamp):
      self._SetLabelData(changed_data)
    # delete missing projects
    if 'ActiveProjectIds' in changed_data:
      projects_to_delete = set(self._projects.keys()) - set(changed_data['ActiveProjectIds']) 
      for project_id in projects_to_delete:
        logging.info("Forgetting deleted project %s", self._projects[project_id].name)
        del self._projects[project_id]
    if 'Projects' in changed_data:
      for project in changed_data['Projects']:
        logging.info("Refreshing data for project %s", project['name'])
        if project['id'] in self._projects:
          logging.info("replacing project data, old timestamp: %s new timestamp: %s",
              self._projects[project['id']].last_updated, project['last_updated'])
        self._projects[project['id']] = Project(project)
    # We have already reloaded project data sent to us.
    # Now any project timestamps that have changed are due to the changes we
    # just sent to the server. Let's update our model.
    if 'ActiveProjectTimestamps' in changed_data:
      for project_id, timestamp in changed_data['ActiveProjectTimestamps'].iteritems():
        # for some reason the project id is a string and not an int here.
        project_id = int(project_id)
        if project_id in self._projects:
          project = self._projects[project_id]
          if project.last_updated != timestamp:
            logging.info("Updating timestamp for project %s to %s",
                project.name, timestamp)
            project.last_updated = timestamp



  def GetProjectMods(self):
    mods = []
    # We need to create the next_action label
    if self._next_action_id == None:
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
        item.labels.append(self._next_action_id)
        mods.append({'type': 'item_update',
                     'timestamp': int(time.time()),
                     'args': {
                       'id': item.item_id,
                       'labels': item.labels
                      }})
        logging.info("add next_action to: %s", item.content)
      for item in state.remove_labels:
        item.labels.remove(self._next_action_id)
        mods.append({'type': 'item_update',
                     'timestamp': int(time.time()),
                     'args': {
                       'id': item.item_id,
                       'labels': item.labels
                      }})
        logging.info("remove next_action from: %s", item.content)
    return mods



def GetResponse():
  values = {'api_token': API_TOKEN}
  data = urllib.urlencode(values)
  req = urllib2.Request('https://api.todoist.com/TodoistSync/v2/get', data)
  return urllib2.urlopen(req)

def DoSync(items_to_sync):
  values = {'api_token': API_TOKEN,
            'items_to_sync': json.dumps(items_to_sync)}
  logging.info("posting %s", values)
  data = urllib.urlencode(values)
  req = urllib2.Request('https://api.todoist.com/TodoistSync/v2/sync', data)
  return urllib2.urlopen(req)

def DoSyncAndGetUpdated(items_to_sync, sync_state):
  values = {'api_token': API_TOKEN,
            'items_to_sync': json.dumps(items_to_sync)}
  for key, value in sync_state.iteritems():
    values[key] = json.dumps(value)
  logging.debug("posting %s", values)
  data = urllib.urlencode(values)
  req = urllib2.Request('https://api.todoist.com/TodoistSync/v2/syncAndGetUpdated', data)
  return urllib2.urlopen(req)

def main():
  logging.basicConfig(level=logging.INFO)
  response = GetResponse()
  json_data = json.loads(response.read())
  logging.debug("Got initial data: %s", json_data)
  a = TodoistData(json_data)
  while True:
    mods = a.GetProjectMods()
    if len(mods) == 0:
      time.sleep(5)
    else:
      logging.info("* Modifications necessary - skipping sleep cycle.")
    logging.info("** Beginning sync")
    sync_state = a.GetSyncState()
    changed_data = DoSyncAndGetUpdated(mods, sync_state).read()
    logging.debug("Got sync data %s", changed_data)
    changed_data = json.loads(changed_data)
    logging.info("* Updating model after receiving sync data")
    a.UpdateChangedData(changed_data)
    logging.info("* Finished updating model")
    logging.info("** Finished sync")

if __name__ == '__main__':
  main()
