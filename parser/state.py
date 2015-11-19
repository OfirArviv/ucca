from collections import deque, defaultdict
from itertools import groupby
from operator import attrgetter
import sys

from action import SHIFT, NODE, IMPLICIT, REDUCE, LEFT_EDGE, RIGHT_EDGE, LEFT_REMOTE, RIGHT_REMOTE, SWAP, FINISH
from config import Config
from ucca.convert import from_text
from ucca import layer0
from ucca import layer1
from ucca import core
from oracle import ROOT_ID


class Node(object):
    """
    Temporary representation for core.Node with only relevant information for parsing
    """
    def __init__(self, index, orig_node=None, text=None, tag=None, implicit=False):
        self.index = index  # Index in the configuration's node list
        self.orig_node = orig_node  # Associated core.Node from the original Passage, during training
        self.node_id = orig_node.ID if orig_node else None  # ID of the original node
        self.text = text  # Text for terminals, None for non-terminals
        self.tag = tag  # During training, the node tag of the original node (Word/Punctuation)
        self.node_index = int(self.node_id.split(core.Node.ID_SEPARATOR)[1]) if orig_node else None
        self.outgoing = []  # Edge list
        self.incoming = []  # Edge list
        self.children = []  # Node list: the children of all edges in outgoing
        self.parents = []  # Node list: the parents of all edges in incoming
        self.node = None  # Associated core.Node, when creating final Passage
        self.implicit = implicit  # True or False
        self.swap_index = self.index  # Used to make sure nodes are not swapped more than once

    def update_swap_index(self, s, buffer):
        """
        Update the node's swap index according to the nodes before and after it.
        Usually the swap index is usually just the index, and that is what it is initialized to.
        If the buffer is not empty and the next node on it is not a terminal, it means that it is
        a non-terminal that was created at some point, probably before this node (because this method
        should be run just when this node is created).
        In that case, the buffer head's index will be lower than this node's index, and we will
        update this node's swap index to be the arithmetic average between the previous node
        (stack top) and the next node (buffer head).
        This will make sure that when we perform the validity check on the SWAP action,
        we will correctly identify this node as always having appearing before b (what is the
        current buffer head). Otherwise, we would prevent swapping this node with b even though
        it should be a legal action (because they have never been swapped before).
        :param s: stack top
        :param buffer: the whole buffer (may be empty)
        """
        if not buffer:
            return
        b = buffer[0]
        if b.text is not None or b.swap_index > self.swap_index:
            return
        self.swap_index = (s.swap_index + b.swap_index) / 2

    def add_to_l1(self, l1, parent, tag, terminals):
        """
        Called when creating final Passage to add a new core.Node
        """
        if Config().verify:
            assert self.node is None or self.text is not None,\
                "Trying to create the same node twice: %s, parent: %s" % (self.node_id, parent)
        edge = self.outgoing[0] if len(self.outgoing) == 1 else None
        if self.text:
            if self.node is None:  # For punctuation, already created by add_punct for parent
                assert parent.node is not None, "Terminal with no parent: \"%s\"" % self
                self.node = parent.node.add(layer1.EdgeTags.Terminal,
                                            terminals[self.index]).child
        elif edge and edge.child.text and layer0.is_punct(terminals[edge.child.index]):
            if Config().verify:
                assert tag == layer1.EdgeTags.Punctuation, "Tag for %s is %s" % (parent.node_id, tag)
                assert edge.tag == layer1.EdgeTags.Terminal, "Tag for %s is %s" % (self.node_id, edge.tag)
            self.node = l1.add_punct(parent.node, terminals[edge.child.index])
            edge.child.node = self.node[0].child
        else:  # The usual case
            self.node = l1.add_fnode(parent.node, tag, implicit=self.implicit)
        if self.node is not None and self.node_id is not None:  # In training, and we have a gold passage
            self.node.extra["remarks"] = self.node_id  # Keep original node ID for reference

    @property
    def is_linkage(self):
        """
        Is this a LKG type node? (During parsing there are no node types)
        """
        return self.outgoing and all(e.tag in (layer1.EdgeTags.LinkRelation,
                                               layer1.EdgeTags.LinkArgument)
                                     for e in self.outgoing)

    @property
    def ancestors(self):
        """
        Find all parents of this node recursively, stopping if a cycle is detected (so self not included)
        """
        parents_ancestors = [d for parent in self.parents for d in parent.ancestors if d is not self]
        return set(self.parents + parents_ancestors)

    @property
    def descendants(self):
        """
        Find all children of this node recursively, stopping if a cycle is detected (so self not included)
        """
        children_descendants = [d for child in self.children for d in child.descendants if d is not self]
        return set(self.children + children_descendants)

    def __repr__(self):
        return Node.__name__ + "(" + str(self.index) + \
               ((", " + self.text) if self.text else "") + \
               ((", " + self.node_id) if self.node_id else "") + ")"

    def __str__(self):
        return '"%s"' % self.text if self.text else self.node_id or str(self.index)

    def __eq__(self, other):
        return self.index == other.index and self.outgoing == other.outgoing

    def __hash__(self):
        return hash((self.index, tuple(self.outgoing)))


class Edge(object):
    """
    Temporary representation for core.Edge with only relevant information for parsing
    """
    def __init__(self, parent, child, tag, remote=False):
        self.parent = parent  # Node object from which this edge comes
        self.child = child  # Node object to which this edge goes
        self.tag = tag  # String tag
        self.remote = remote  # True or False

    def add(self):
        assert self.tag is not None, "No tag given for new edge %s -> %s" % (self.parent, self.child)
        assert self.parent is not self.child, "Trying to create self-loop edge on %s" % self.parent
        if Config().verify:
            assert self not in self.parent.outgoing, "Trying to create outgoing edge twice: %s" % self
            assert self not in self.child.incoming, "Trying to create incoming edge twice: %s" % self
            for d in self.child.descendants:
                assert self.parent not in d.children, "Detected cycle created by edge: %s" % self
        self.parent.outgoing.append(self)
        self.parent.children.append(self.child)
        self.child.incoming.append(self)
        self.child.parents.append(self.parent)

    def __repr__(self):
        return Edge.__name__ + "(" + self.tag + ", " + self.parent + ", " + self.child +\
               ((", " + str(self.remote)) if self.remote else "") + ")"

    def __str__(self):
        return "%s -%s-> %s%s" % (self.parent, self.tag, self.child,
                                  " (remote)" if self.remote else "")

    def __eq__(self, other):
        return self.parent.index == other.parent.index and self.child == other.child and \
               self.tag == other.tag and self.remote == other.remote

    def __hash__(self):
        return hash((self.parent.index, self.child.index, self.tag))


class State(object):
    """
    The parser's state, responsible for applying actions and creating the final Passage
    :param passage: a Passage object to get the tokens from, or a list of lists of strings
    :param passage_id: the ID of the passage to generate
    :param callback: function to call after creating the list of nodes (e.g. POS tagger)
    """
    def __init__(self, passage, passage_id, callback=None):
        self.log = []
        self.finished = False
        self.passage = isinstance(passage, core.Passage)
        if self.passage:  # During training or evaluation, create from gold Passage
            self.nodes = [Node(i, orig_node=x, text=x.text, tag=x.tag) for i, x in
                          enumerate(passage.layer(layer0.LAYER_ID).all)]
            self.tokens = [[terminal.text for terminal in terminals]
                           for _, terminals in groupby(passage.layer(layer0.LAYER_ID).all,
                                                       key=attrgetter('paragraph'))]
            root_node = passage.by_id(ROOT_ID)
        else:  # During parsing, create from plain text: assume passage is list of lists of strings
            self.tokens = passage
            self.nodes = [Node(i, text=token) for i, token in
                          enumerate(token for paragraph in passage for token in paragraph)]
            root_node = None
        if callback is not None:
            callback(self)
        self.terminals = list(self.nodes)
        self.buffer = deque(self.nodes)
        self.root = self.add_node(root_node)  # The root is not part of the stack/buffer
        self.stack = [self.root]
        self.passage_id = passage_id

    def is_valid(self, action):
        """
        :param action: action to check for validity
        :return: is the action (including tag) valid in the current state?
        """
        try:
            self.assert_valid(action)
        except AssertionError:
            return False
        return True

    def assert_valid(self, action):
        """
        Raise AssertionError if the action is invalid in the current state
        :param action: action to check for validity
        """
        if action.is_type(FINISH):
            assert self.root.outgoing, "Root must have at least one child at the end of the parse"
            for terminal in self.terminals:
                assert terminal.incoming, "Every terminal must have a parent at the end of the parse"
        elif action.is_type(SHIFT):
            assert self.buffer, "Buffer must not be empty in order to shift from it"
        else:
            assert self.stack, "Action requires non-empty stack: %s" % action
            s0 = self.stack[-1]
            if action.is_type(NODE):
                assert s0 is not self.root, "The root may not have parents"
                assert (s0.text is not None) == (action.tag == layer1.EdgeTags.Terminal), \
                    "Edge tag must be T iff child is terminal"
                # TODO check if it's ok to assert that s0 has no parents
                # or possibly that it has no incoming edges with action.tag
                self.assert_node_ratio()
            elif action.is_type(IMPLICIT):
                assert s0.text is None, "Terminals may not have (implicit) children"
                assert not s0.implicit, "Implicit node loop"
                self.assert_node_ratio()
            elif action.is_type(REDUCE):
                assert s0 is not self.root or s0.outgoing, "May not reduce the root without children"
            else:
                assert len(self.stack) > 1, "Action requires at least two stack elements: %s" % action
                if action.is_type(LEFT_EDGE, LEFT_REMOTE, RIGHT_EDGE, RIGHT_REMOTE):
                    parent, child = self.get_parent_child(action)
                    assert child is not self.root, "Root may not be the child"
                    assert parent.text is None, "Terminal may not be the parent"
                    assert parent is not self.root or child.text is None, "root->terminal edge"
                    assert child not in parent.children, "Edge must not already exist"
                    assert (child.text is not None) == (action.tag == layer1.EdgeTags.Terminal), \
                        "Edge tag must be T iff child is terminal"
                    for d in child.descendants:
                        assert parent not in d.children, "Detected cycle created by edge: %s" % self
                    # Include this (instead of child not in children) to allow multiple edges between nodes:
                    # (as long as their tags are different)
                    # assert self.create_edge(action) not in parent.outgoing, "Edge must not already exist"
                elif action.is_type(SWAP):
                    # A regular swap is possible since the stack has at least two elements;
                    # A compound swap is possible if the stack is longer than the distance
                    distance = action.tag or 1
                    assert 1 <= distance < len(self.stack), "Invalid swap distance: %d" % distance
                    swapped = self.stack[-distance - 1]
                    assert s0.text is None or swapped.text is None, "Swapping terminals is not allowed"
                    # To prevent swap loops: only swap if the nodes are currently in their original order
                    assert s0.text is not None or swapped.swap_index <= s0.swap_index, \
                        "Swapping already-swapped nodes"
                else:
                    raise Exception("Invalid action: %s" % action)

    def transition(self, action):
        """
        Main part of the parser: apply action given by oracle or classifier
        :param action: Action object to apply
        """
        self.log = []
        if action.is_type(SHIFT):  # Push buffer head to stack; shift buffer
            self.stack.append(self.buffer.popleft())
        elif action.is_type(NODE):  # Create new parent node and add to the buffer
            parent = self.add_node(action.orig_node)
            parent.update_swap_index(self.stack[-1], self.buffer)
            self.add_edge(Edge(parent, self.stack[-1], action.tag))
            self.buffer.appendleft(parent)
        elif action.is_type(IMPLICIT):  # Create new child node and add to the buffer
            child = self.add_node(action.orig_node, implicit=True)
            child.update_swap_index(self.stack[-1], self.buffer)
            self.add_edge(Edge(self.stack[-1], child, action.tag))
            self.buffer.appendleft(child)
        elif action.is_type(REDUCE):  # Pop stack (no more edges to create with this node)
            self.stack.pop()
        elif action.is_type(LEFT_EDGE, LEFT_REMOTE, RIGHT_EDGE, RIGHT_REMOTE):
            parent, child = self.get_parent_child(action)
            self.add_edge(Edge(parent, child, action.tag, remote=action.remote))
        elif action.is_type(SWAP):  # Place second (or more) stack item back on the buffer
            distance = action.tag or 1
            s = slice(-distance - 1, -1)
            self.log.append("%s <--> %s" % (", ".join(map(str, self.stack[s])), self.stack[-1]))
            self.buffer.extendleft(reversed(self.stack[s]))  # extendleft reverses the order
            del self.stack[s]
        elif action.is_type(FINISH):  # Nothing left to do
            self.finished = True
        else:
            raise Exception("Invalid action: " + action)
        if Config().verify:
            intersection = set(self.stack).intersection(self.buffer)
            assert not intersection, "Stack and buffer overlap: %s" % intersection

    def add_node(self, *args, **kwargs):
        """
        Called during parsing to add a new Node (not core.Node) to the temporary representation
        :param args: ordinal arguments for Node()
        :param kwargs: keyword arguments for Node()
        """
        node = Node(len(self.nodes), *args, **kwargs)
        if Config().verify:
            assert node not in self.nodes, "Node already exists"
        self.nodes.append(node)
        self.log.append("node: %s" % node)
        return node

    def add_edge(self, edge):
        edge.add()
        self.log.append("edge: %s" % edge)

    def get_parent_child(self, action):
        if action.is_type(LEFT_EDGE, LEFT_REMOTE):
            return self.stack[-1], self.stack[-2]
        elif action.is_type(RIGHT_EDGE, RIGHT_REMOTE):
            return self.stack[-2], self.stack[-1]
        else:
            return None, None

    def create_passage(self):
        """
        Create final passage from temporary representation
        :return: core.Passage created from self.nodes
        """
        paragraphs = [" ".join(paragraph) for paragraph in self.tokens]
        passage = from_text(paragraphs, self.passage_id)
        terminals = passage.layer(layer0.LAYER_ID).all
        l1 = layer1.Layer1(passage)
        if self.passage:  # We are in training and we have a gold passage
            passage.nodes[ROOT_ID].extra["remarks"] = self.root.node_id  # For reference
            self.fix_terminal_tags(terminals)
        remotes = []  # To be handled after all nodes are created
        linkages = []  # To be handled after all non-linkage nodes are created
        self.topological_sort()  # Sort self.nodes
        for node in self.nodes:
            if self.passage and Config().verify:
                assert node.text or node.outgoing or node.implicit, "Non-terminal leaf node: %s" % node
                assert node.node or node is self.root or node.is_linkage, "Non-root without incoming: %s" % node
            if node.is_linkage:
                linkages.append(node)
            else:
                for edge in node.outgoing:
                    if edge.remote:
                        remotes.append((node, edge))
                    else:
                        edge.child.add_to_l1(l1, node, edge.tag, terminals)

        for node, edge in remotes:  # Add remote edges
            node.node.add(edge.tag, edge.child.node, edge_attrib={"remote": True})

        for node in linkages:  # Add linkage nodes and edges
            link_relation = None
            link_args = []
            for edge in node.outgoing:
                if edge.tag == layer1.EdgeTags.LinkRelation:
                    assert link_relation is None, "Multiple link relations: %s, %s" % (link_relation, edge.child.node)
                    link_relation = edge.child.node
                elif edge.tag == layer1.EdgeTags.LinkArgument:
                    link_args.append(edge.child.node)
            assert link_relation is not None, "No link relations: %s" % node
            if len(link_args) < 2:
                print("Less than two link arguments for linkage %s" % node, file=sys.stderr)
            node.node = l1.add_linkage(link_relation, *link_args)
            if node.node_id:  # We are in training and we have a gold passage
                node.node.extra["remarks"] = node.node_id  # For reference

        return passage

    def fix_terminal_tags(self, terminals):
        for terminal, orig_terminal in zip(terminals, self.terminals):
            if terminal.tag != orig_terminal.tag:
                if Config().verbose:
                    print("%s is the wrong tag for terminal: %s" % (terminal.tag, terminal.text),
                          file=sys.stderr)
                terminal.tag = orig_terminal.tag

    def topological_sort(self):
        """
        Sort self.nodes topologically, each node appearing as early as possible
        Also sort each node's outgoing and incoming edge according to the node order
        """
        levels = defaultdict(list)
        level_by_index = {}
        stack = [node for node in self.nodes if not node.outgoing]
        while stack:
            node = stack.pop()
            if node.index not in level_by_index:
                parents = [edge.parent for edge in node.incoming]
                if parents:
                    unexplored_parents = [parent for parent in parents
                                          if parent.index not in level_by_index]
                    if unexplored_parents:
                        for parent in unexplored_parents:
                            stack.append(node)
                            stack.append(parent)
                    else:
                        level = 1 + max(level_by_index[parent.index] for parent in parents)
                        levels[level].append(node)
                        level_by_index[node.index] = level
                else:
                    levels[0].append(node)
                    level_by_index[node.index] = 0
        self.nodes = [node for level, level_nodes in sorted(levels.items())
                      for node in sorted(level_nodes, key=lambda x: x.node_index or x.index)]
        for node in self.nodes:
            node.outgoing.sort(key=lambda x: x.child.node_index or self.nodes.index(x.child))
            node.incoming.sort(key=lambda x: x.parent.node_index or self.nodes.index(x.parent))

    def assert_node_ratio(self):
        ratio = len(self.nodes) / len(self.terminals) - 1
        max_ratio = Config().max_nodes_ratio
        assert ratio <= max_ratio, "Reached maximum ratio (%.3f) of non-terminals to terminals" % max_ratio

    def str(self, sep):
        return "stack: [%-20s]%sbuffer: [%s]" % (" ".join(map(str, self.stack)), sep,
                                                 " ".join(map(str, self.buffer)))

    def __str__(self):
        return self.str(" ")

    def __eq__(self, other):
        return self.stack == other.stack and self.buffer == other.buffer and \
               self.nodes == other.nodes

    def __hash__(self):
        return hash((tuple(self.stack), tuple(self.buffer), tuple(self.nodes)))