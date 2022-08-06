"""
Making DAGs

In it's simplest form, consider this:

>>> from meshed import DAG
>>>
>>> def this(a, b=1):
...     return a + b
...
>>> def that(x, b=1):
...     return x * b
...
>>> def combine(this, that):
...     return (this, that)
...
>>>
>>> dag = DAG((this, that, combine))
>>> print(dag.synopsis_string())
x,b -> that_ -> that
a,b -> this_ -> this
this,that -> combine_ -> combine

But don't be fooled: There's much more to it!


FAQ and Troubleshooting
=======================

DAGs and Pipelines
------------------

>>> from functools import partial
>>> from meshed import DAG
>>> def chunker(sequence, chk_size: int):
...     return zip(*[iter(sequence)] * chk_size)
>>>
>>> my_chunker = partial(chunker, chk_size=3)
>>>
>>> vec = range(8)  # when appropriate, use easier to read sequences
>>> list(my_chunker(vec))
[(0, 1, 2), (3, 4, 5)]

Oh, that's just a ``my_chunker -> list`` pipeline!
A pipeline is a subset of DAG, so let me do this:

>>> dag = DAG([my_chunker, list])
>>> dag(vec)
Traceback (most recent call last):
...
TypeError: missing a required argument: 'sequence'

What happened here?
You're assuming that saying ``[my_chunker, list]`` is enough for DAG to know that
what you meant is for ``my_chunker`` to feed it's input to ``list``.
Sure, DAG has enough information to do so, but the default connection policy doesn't
assume that it's a pipeline you want to make.
In fact, the order you specify the functions doesn't have an affect on the connections
with the default connection policy.

See what the signature of ``dag`` is:

>>> from inspect import signature
>>> str(signature(dag))
'(iterable=(), /, sequence, *, chk_size: int = 3)'

So dag actually works just fine. Here's the proof:

>>> dag([1,2,3], vec)  # doctest: +SKIP
([1, 2, 3], <zip object at 0x104d7f080>)

It's just not what you might have intended.

Your best bet to get what you intended is to be explicit.

The way to be explicit is to not specify functions alone, but ``FuncNodes`` that
wrap them, along with the specification
the ``name`` the function will be referred to by,
the names that it's parameters should ``bind`` to (that is, where the function
will get it's import arguments from), and
the ``out`` name of where it should be it's output.

In the current case a fully specified DAG would look something like this:

>>> from meshed import FuncNode
>>> dag = DAG(
...     [
...         FuncNode(
...             func=my_chunker,
...             name='chunker',
...             bind=dict(sequence='sequence', chk_size='chk_size'),
...             out='chks'
...         ),
...         FuncNode(
...             func=list,
...             name='gather_chks_into_list',
...             bind=dict(iterable='chks'),
...             out='list_of_chks'
...         ),
...     ]
... )
>>> list(dag(vec))
[(0, 1, 2), (3, 4, 5)]

But really, if you didn't care about the names of things,
all you need in this case was to make sure that the output of ``my_chunker`` was
fed to ``list``, and therefore the following was sufficient:

>>> dag = DAG([
...     FuncNode(my_chunker, out='chks'),  # call the output of chunker "chks"
...     FuncNode(list, bind=dict(iterable='chks'))  # source list input from "chks"
... ])
>>> list(dag(vec))
[(0, 1, 2), (3, 4, 5)]

Connection policies are very useful when you want to define ways for DAG to
"just figure it out" for you.
That is, you want to tell the machine to adapt to your thoughts, not vice versa.
We support such technological expectations!
The default connection policy is there to provide one such ways, but
by all means, use another!

Does this mean that connection policies are not for production code?
Well, it depends. The Zen of Python (``import this``)
states "explicit is better than implicit", and indeed it's often
a good fallback rule.
But defining components and the way they should be assembled can go a long way
in achieving consistency, separation of concerns, adaptability, and flexibility.
All quite useful things. Also in production. Especially in production.
That said it is your responsiblity to use the right policy for your particular context.

"""

from functools import partial, wraps, cached_property
from collections import defaultdict

from dataclasses import dataclass, field
from itertools import chain
from operator import attrgetter
from typing import (
    Callable,
    MutableMapping,
    Union,
    Optional,
    Iterable,
    Any,
    Mapping,
)
from warnings import warn

from i2 import double_up_as_factory
from i2.signatures import (
    call_somewhat_forgivingly,
    Parameter,
    empty,
    Sig,
    sort_params,
)
from meshed.base import (
    FuncNode,
    validate_that_func_node_names_are_sane,
    _mk_func_nodes,
    _func_nodes_to_graph_dict,
    is_func_node,
    FuncNodeAble,
    func_node_transformer,
)

from meshed.util import (
    lambda_name,
    ValidationError,
    NotUniqueError,
    NotFound,
    NameValidationError,
    Renamer,
    _if_none_return_input,
    numbered_suffix_renamer,
    replace_item_in_iterable,
    InvalidFunctionParameters,
    extract_values,
    extract_items,
    ParameterMerger,
    conservative_parameter_merge,
)
from meshed.itools import (
    topological_sort,
    leaf_nodes,
    root_nodes,
    descendants,
    ancestors,
)


def order_subset_from_list(items, sublist):
    assert set(sublist).issubset(set(items)), f'{sublist} is not contained in {items}'
    d = {k: v for v, k in enumerate(items)}

    return sorted(sublist, key=lambda x: d[x])


def find_first_free_name(prefix, exclude_names=(), start_at=2):
    if prefix not in exclude_names:
        return prefix
    else:
        i = start_at
        while True:
            name = f'{prefix}__{i}'
            if name not in exclude_names:
                return name
            i += 1


def mk_mock_funcnode(arg, out):
    @Sig(arg)
    def func():
        pass

    # name = "_mock_" + str(arg) + "_" + str(out)  # f-string
    name = f'_mock_{str(arg)}_{str(out)}'  # f-string

    return FuncNode(func=func, out=out, name=name)


def mk_func_name(func, exclude_names=()):
    name = getattr(func, '__name__', '')
    if name == '<lambda>':
        name = lambda_name()  # make a lambda name that is a unique identifier
    elif name == '':
        if isinstance(func, partial):
            return mk_func_name(func.func, exclude_names)
        else:
            raise NameValidationError(f"Can't make a name for func: {func}")
    return find_first_free_name(name, exclude_names)


def arg_names(func, func_name, exclude_names=()):
    names = Sig(func).names

    def gen():
        _exclude_names = exclude_names
        for name in names:
            if name not in _exclude_names:
                yield name
            else:
                found_name = find_first_free_name(
                    f'{func_name}__{name}', _exclude_names
                )
                yield found_name
                _exclude_names = _exclude_names + (found_name,)

    return list(gen())


def named_partial(func, *args, __name__=None, **keywords):
    """functools.partial, but with a __name__

    >>> f = named_partial(print, sep='\\n')
    >>> f.__name__
    'print'

    >>> f = named_partial(print, sep='\\n', __name__='now_partial_has_a_name')
    >>> f.__name__
    'now_partial_has_a_name'
    """
    f = partial(func, *args, **keywords)
    f.__name__ = __name__ or func.__name__
    return f


from i2.signatures import ch_func_to_all_pk


def hook_up(func, variables: MutableMapping, output_name=None):
    """Source inputs and write outputs to given variables mapping.

    Returns inputless and outputless function that will, when called,
    get relevant inputs from the provided variables mapping and write it's
    output there as well.

    :param variables: The MutableMapping (like... a dict) where the function
    should both read it's input and write it's output.
    :param output_name: The key of the variables mapping that should be used
    to write the output of the function
    :return: A function

    >>> def formula1(w, /, x: float, y=1, *, z: int = 1):
    ...     return ((w + x) * y) ** z

    >>> d = {}
    >>> f = hook_up(formula1, d)
    >>> # NOTE: update d, not d = dict(...), which would make a DIFFERENT d
    >>> d.update(w=2, x=3, y=4)  # not d = dict(w=2, x=3, y=4), which would
    >>> f()

    Note that there's no output. The output is in d
    >>> d
    {'w': 2, 'x': 3, 'y': 4, 'formula1': 20}

    Again...

    >>> d.clear()
    >>> d.update(w=1, x=2, y=3)
    >>> f()
    >>> d['formula1']
    9

    """
    _func = ch_func_to_all_pk(func)  # makes a position-keyword copy of func
    output_key = output_name
    if output_name is None:
        output_key = _func.__name__

    def source_from_decorated():
        variables[output_key] = call_somewhat_forgivingly(_func, (), variables)

    return source_from_decorated


def _separate_func_nodes_and_var_nodes(nodes):
    func_nodes = list()
    var_nodes = list()
    for node in nodes:
        if is_func_node(node):
            func_nodes.append(node)
        else:
            var_nodes.append(node)
    return func_nodes, var_nodes


_not_found = object()


def _find_unique_element(item, search_iterable, key: Callable[[Any, Any], bool]):
    """Find item in search_iterable, using key as the matching function,
    raising a NotFound error if no match and a NotUniqueError if more than one."""
    it = filter(lambda x: key(item, x), search_iterable)
    first = next(it, _not_found)
    if first == _not_found:
        raise NotFound(f"{item} wasn't found")
    else:
        the_next_match = next(it, _not_found)
        if the_next_match is not _not_found:
            raise NotUniqueError(f"{item} wasn't unique")
    return first


def modified_func_node(func_node, **modifications) -> FuncNode:
    modifiable_attrs = {'func', 'name', 'bind', 'out'}
    assert not modifications.keys().isdisjoint(
        modifiable_attrs
    ), f"Can only modify these: {', '.join(modifiable_attrs)}"
    original_func_node_kwargs = {
        'func': func_node.func,
        'name': func_node.name,
        'bind': func_node.bind,
        'out': func_node.out,
    }
    return FuncNode(**dict(original_func_node_kwargs, **modifications))


from i2 import partialx

# TODO: doctests
def partialized_funcnodes(func_nodes, **keyword_defaults):
    for func_node in func_nodes:
        if argnames_to_be_bound := set(keyword_defaults).intersection(
            func_node.sig.names
        ):
            bindings = dict(extract_items(keyword_defaults, argnames_to_be_bound))
            # partialize the func and move defaulted params to the end
            partialized_func = partialx(
                func_node.func, **bindings, _allow_reordering=True
            )
            # get rid of kinds  # TODO: This is a bit extreme -- consider gentler touch
            nice_kinds_sig = Sig(partialized_func).ch_kinds_to_position_or_keyword()
            nice_kinds_partialized_func = nice_kinds_sig(partialized_func)
            yield modified_func_node(
                func_node, func=nice_kinds_partialized_func
            )  # TODO: A better way without partial?
        else:
            yield func_node


Scope = dict
VarNames = Iterable[str]
DagOutput = Any


def _name_attr_or_x(x):
    return getattr(x, 'name', x)


def change_value_on_cond(d, cond, func):
    for k, v in d.items():
        if cond(k, v):
            d[k] = func(v)
    return d


# TODO: caching last scope isn't really the DAG's direct concern -- it's a debugging
#  concern. Perhaps a more general form would be to define a cache factory defaulting
#  to a dict, but that could be a "dict" that logs writes (even to an attribute of self)
@dataclass
class DAG:
    """
    >>> from meshed.dag import DAG, Sig
    >>>
    >>> def this(a, b=1):
    ...     return a + b
    >>> def that(x, b=1):
    ...     return x * b
    >>> def combine(this, that):
    ...     return (this, that)
    >>>
    >>> dag = DAG((this, that, combine))
    >>> print(dag.synopsis_string())
    x,b -> that_ -> that
    a,b -> this_ -> this
    this,that -> combine_ -> combine

    But what does it do?

    It's a callable, with a signature:

    >>> Sig(dag)  # doctest: +SKIP
    <Sig (x, a, b=1)>

    And when you call it, it executes the dag from the root values you give it and
    returns the leaf output values.

    >>> dag(1, 2, 3)  # (a+b,x*b) == (2+3,1*3) == (5, 3)
    (5, 3)
    >>> dag(1, 2)  # (a+b,x*b) == (2+1,1*1) == (3, 1)
    (3, 1)

    The above DAG was created straight from the functions, using only the names of the
    functions and their arguments to define how to hook the network up.

    But if you didn't write those functions specifically for that purpose, or you want
    to use someone else's functions, we got you covered.

    You can define the name of the node (the `name` argument), the name of the output
    (the `out` argument) and a mapping from the function's arguments names to
    "network names" (through the `bind` argument).
    The edges of the DAG are defined by matching `out` TO `bind`.

    """

    func_nodes: Iterable[Union[FuncNode, Callable]] = ()
    cache_last_scope: bool = field(default=True, repr=False)
    parameter_merge: ParameterMerger = field(
        default=conservative_parameter_merge, repr=False
    )
    # can return a prepopulated scope too!
    new_scope: Callable = field(default=dict, repr=False)
    name: str = None
    extract_output_from_scope: Callable[[Scope, VarNames], DagOutput] = field(
        default=extract_values, repr=False
    )


    def __post_init__(self):
        self.func_nodes = tuple(_mk_func_nodes(self.func_nodes))
        self.graph = _func_nodes_to_graph_dict(self.func_nodes)
        self.nodes = topological_sort(self.graph)
        # reorder the nodes to fit topological order
        self.func_nodes, self.var_nodes = _separate_func_nodes_and_var_nodes(self.nodes)
        # self.sig = Sig(dict(extract_items(sig.parameters, 'xz')))
        self.__signature__ = Sig(  # make a signature
            sort_params(  # with the sorted params (sorted to satisfy kind/default order)
                self.src_name_params(root_nodes(self.graph))
            )
        )

        # self.__signature__(self)  # to put the signature on the callable DAG
        # figure out the roots and leaves
        self.roots = tuple(
            self.__signature__.names
        )  # roots in the same order as signature
        leafs = leaf_nodes(self.graph)
        # But we want leafs in topological order
        self.leafs = tuple([name for name in self.nodes if name in leafs])
        self.last_scope = None
        if self.name is not None:
            self.__name__ = self.name

        self.bindings_cleaner()

    def bindings_cleaner(self):
        funcnodes_names = [node.name for node in self.func_nodes]
        func = lambda v: self._func_node_for[v].out
        cond = lambda k, v: v in funcnodes_names
        for node in self.func_nodes:
            node.bind = change_value_on_cond(node.bind, cond, func)

    def __call__(self, *args, **kwargs):
        return self._call(*args, **kwargs)

    def _call(self, *args, **kwargs):
        # Get a dict of {argname: argval} pairs from positional and keyword arguments
        # How positionals are resolved is determined by sels.sig
        # The result is the initial ``scope`` the func nodes will both read from
        # to get their arguments, and write their outputs to.
        scope = self.__signature__.kwargs_from_args_and_kwargs(
            args, kwargs, apply_defaults=True
        )
        # Go through self.func_nodes in order and call them on scope (performing said
        # read_input -> call_func -> write_output operations)
        self.call_on_scope(scope)
        # From the scope, that may contain all intermediary results,
        # extract the desired final output and return it
        return self.extract_output_from_scope(scope, self.leafs)

    def call_on_scope(self, scope=None):
        """Calls the func_nodes using scope (a dict or MutableMapping) both to
        source it's arguments and write it's results.

        Note: This method is only meant to be used as a backend to __call__, not as
        an actual interface method. Additional control/constraints on read and writes
        can be implemented by providing a custom scope for that. For example, one could
        log read and/or writes to specific keys, or disallow overwriting to an existing
        key (useful for pipeline sanity), etc.
        """
        if scope is None:
            scope = self.new_scope()  # fresh new scope
        if self.cache_last_scope:
            self.last_scope = scope  # just to remember it, for debugging purposes ONLY!

        for func_node in self.func_nodes:
            func_node.call_on_scope(scope)

    # def clone(self, *args, **kwargs):
    #     """Use args, kwargs to make an instance, using self attributes for
    #     unspecified arguments.
    #     """

    def __getitem__(self, item):
        """Get a sub-dag from a specification of (var or fun) input and output nodes.

        ``dag[input_nodes:output_nodes]`` is the sub-dag made of intersection of all
        descendants of ``input_nodes``
        (inclusive) and ancestors of ``output_nodes`` (inclusive), where additionally,
        when a func node is contained, it takes with it the input and output nodes
        it needs.

        >>> def f(a): ...
        >>> def g(f): ...
        >>> def h(g): ...
        >>> def i(h): ...
        >>> dag = DAG([f, g, h, i])

        See what this dag looks like (it's a simple pipeline):

        >>> dag = DAG([f, g, h, i])
        >>> print(dag.synopsis_string())
        a -> f_ -> f
        f -> g_ -> g
        g -> h_ -> h
        h -> i_ -> i

        Get a subdag from ``g_`` (indicates the function here) to the end of ``dag``

        >>> subdag = dag['g_':]
        >>> print(subdag.synopsis_string())
        f -> g_ -> g
        g -> h_ -> h
        h -> i_ -> i

        From the beginning to ``h_``

        >>> print(dag[:'h_'].synopsis_string())
        a -> f_ -> f
        f -> g_ -> g
        g -> h_ -> h

        From ``g_`` to ``h_`` (both inclusive)

        >>> print(dag['g_':'h_'].synopsis_string())
        f -> g_ -> g
        g -> h_ -> h

        Above we used function (node names) to specify what we wanted, but we can also
        use names of input/output var-nodes. Do note the difference though.
        The nodes you specify to get a sub-dag are INCLUSIVE, but when you
        specify function nodes, you also get the input and output nodes of these
        functions.

        The ``dag['g_', 'h_']`` give us a sub-dag starting at ``f`` (the input node),
        but when we ask ``dag['g', 'h_']`` instead, ``g`` being the output node of
        function node ``g_``, we only get ``g -> h_ -> h``:

        >>> print(dag['g':'h'].synopsis_string())
        g -> h_ -> h

        If we wanted to include ``f`` we'd have to specify it:

        >>> print(dag['f':'h'].synopsis_string())
        f -> g_ -> g
        g -> h_ -> h

        Those were for simple pipelines, but let's now look at a more complex dag.

        We'll let the following examples self-comment:

        >>> def f(u, v): ...
        >>> def g(f): ...
        >>> def h(f, w): ...
        >>> def i(g, h): ...
        >>> def j(h, x): ...
        >>> def k(i): ...
        >>> def l(i, j): ...
        >>> dag = DAG([f, g, h, i, j, k, l])
        >>> print(dag.synopsis_string())
        u,v -> f_ -> f
        f,w -> h_ -> h
        h,x -> j_ -> j
        f -> g_ -> g
        g,h -> i_ -> i
        i,j -> l_ -> l
        i -> k_ -> k

        A little util to get consistent prints:

        >>> def print_sorted_synopsis(dag):
        ...     t = sorted(dag.synopsis_string().split('\\n'))
        ...     print('\\n'.join(t))

        >>> print_sorted_synopsis(dag[['u', 'f']:'h'])
        f,w -> h_ -> h
        u,v -> f_ -> f
        >>> print_sorted_synopsis(dag['u':'h'])
        f,w -> h_ -> h
        u,v -> f_ -> f
        >>> print_sorted_synopsis(dag[['u', 'f']:['h', 'g']])
        f -> g_ -> g
        f,w -> h_ -> h
        u,v -> f_ -> f
        >>> print_sorted_synopsis(dag[['x', 'g']:'k'])
        g,h -> i_ -> i
        i -> k_ -> k
        >>> print_sorted_synopsis(dag[['x', 'g']:['l', 'k']])
        g,h -> i_ -> i
        h,x -> j_ -> j
        i -> k_ -> k
        i,j -> l_ -> l

        >>>

        """
        return self._getitem(item)

    def _getitem(self, item):
        ins, outs = self.process_item(item)
        _descendants = set(
            filter(FuncNode.has_as_instance, set(ins) | descendants(self.graph, ins))
        )
        _ancestors = set(
            filter(FuncNode.has_as_instance, set(outs) | ancestors(self.graph, outs))
        )
        subgraph_nodes = _descendants.intersection(_ancestors)
        # TODO: When clone ready, use to do `constructor = type(self)` instead of DAG
        # constructor = type(self)  # instead of DAG
        initial_nodes = self.func_nodes
        ordered_subgraph_nodes = order_subset_from_list(initial_nodes, subgraph_nodes)

        return DAG(
            func_nodes=ordered_subgraph_nodes,
            cache_last_scope=self.cache_last_scope,
            parameter_merge=self.parameter_merge,
        )

    # TODO: Think about adding a ``_roll_in_orphaned_nodes=False`` argument:
    #   See https://github.com/i2mint/meshed/issues/14 for more information.
    def partial(
        self,
        *positional_dflts,
        _remove_bound_arguments=False,
        _consider_defaulted_arguments_as_bound=False,
        **keyword_dflts,
    ):
        """Get a curried version of the DAG.

        Like ``functools.partial``, but returns a DAG (not just a callable) and allows
        you to remove bound arguments as well as roll in orphaned_nodes.

        :param positional_dflts: Bind arguments positionally
        :param keyword_dflts: Bind arguments through their names
        :param _remove_bound_arguments: False -- set to True if you don't want bound
            arguments to show up in the signature.
        :param _consider_defaulted_arguments_as_bound: False -- set to True if
            you want to also consider arguments that already had defaults as bound
            (and be removed).
        :return:

        >>> def f(a, b):
        ...     return a + b
        >>> def g(c, d=4):
        ...     return c * d
        >>> def h(f, g):
        ...     return g - f
        >>> dag = DAG([f, g, h])
        >>> from inspect import signature
        >>> str(signature(dag))
        '(c, a, b, d=4)'
        >>> dag(3, 1, 2, 4)  # == (3 * 4) - (1 + 2) == 12 - 3 == 9
        9
        >>> dag(c=3, a=1, b=2, d=4)  # same as above
        9

        >>> new_dag = dag.partial(c=3)
        >>> isinstance(new_dag, DAG)  # it's a dag (not just a partialized callable!)
        True
        >>> str(signature(new_dag))
        '(a, b, c=3, d=4)'
        >>> new_dag(1, 2)  # same as dag(c=3, a=1, b=2, d=4), so:
        9
        """

        keyword_dflts = self.__signature__.kwargs_from_args_and_kwargs(
            args=positional_dflts,
            kwargs=keyword_dflts,
            apply_defaults=_consider_defaulted_arguments_as_bound,
            # positional_dflts and keyword_dflts usually don't cover all arguments, so:
            allow_partial=True,
            # we prefer to let the user know if they're trying to bind arguments
            # that don't exist, so:
            allow_excess=False,
            # we don't really care about kind, so:
            ignore_kind=True,
        )
        # TODO: mk_instance: What about other init args (cache_last_scope, ...)?
        mk_instance = type(self)
        func_nodes = partialized_funcnodes(self, **keyword_dflts)
        new_dag = mk_instance(func_nodes)
        if _remove_bound_arguments:
            new_sig = Sig(new_dag).remove_names(list(keyword_dflts))
            new_sig(new_dag)  # Change the signature of new_dag with bound args removed
        return new_dag

    def process_item(self, item):
        assert isinstance(item, slice), f'must be a slice, was: {item}'

        input_names, outs = item.start, item.stop

        empty_slice = slice(None)

        def ensure_variable_list(obj):
            if obj is None:
                return self.var_nodes
            if isinstance(obj, str):
                obj = obj.split()
            if isinstance(obj, (str, Callable)):
                # TODO: See if we can use _func_node_for instead
                return [self.get_node_matching(obj)]
            elif isinstance(obj, Iterable):
                # TODO: See if we can use _func_node_for instead
                return list(map(self.get_node_matching, obj))
            else:
                raise ValidationError(f'Unrecognized variables specification: {obj}')

        # assert len(item) == 2, f"Only items of size 1 or 2 are supported"
        input_names, outs = map(ensure_variable_list, [input_names, outs])
        return input_names, outs

    def get_node_matching(self, idx):
        if isinstance(idx, str):
            if idx in self.var_nodes:
                return idx
            return self._func_node_for[idx]
        elif isinstance(idx, Callable):
            return self._func_node_for[idx]
        raise NotFound(f'No matching node for idx: {idx}')

    # TODO: Reflect: Should we include functions as keys here? Makes existence of the
    #  item depend on unicity of the function in the DAG, therefore dynamic, so instable?
    @cached_property
    def _func_node_for(self):
        """A dictionary mapping identifiers and functions to their FuncNode instances
        in the DAG. The keys of this dictionary will include:

        - identifiers (names) of the ``FuncNode`` instances
        - ``out`` of ``FuncNode`` instances
        - The ``.func`` of the ``FuncNode`` instances if it's unique.

        >>> def foo(x): return x + 1
        >>> def bar(x): return x * 2
        >>> dag = DAG([
        ...     FuncNode(foo, out='foo_output'),
        ...     FuncNode(bar, name='B', out='b', bind={'x': 'foo_output'}),
        ... ])

        A ``FuncNode`` instance is indexed by both its identifier (``.name``) as well as
        the identifier of it's output (``.out``):

        >>> dag._func_node_for['foo_output']
        FuncNode(x -> foo -> foo_output)
        >>> dag._func_node_for['foo']
        FuncNode(x -> foo -> foo_output)
        >>> dag._func_node_for['b']
        FuncNode(x=foo_output -> B -> b)
        >>> dag._func_node_for['B']
        FuncNode(x=foo_output -> B -> b)

        If the function is hashable (most are) and unique within the ``DAG``, you
        can also find the ``FuncNode`` via the ``.func`` it's wrapping:

        >>> dag._func_node_for[foo]
        FuncNode(x -> foo -> foo_output)
        >>> dag._func_node_for[bar]
        FuncNode(x=foo_output -> B -> b)

        A word of warning though: The function index is provided as a convenience, but
        using identifiers is preferable since referencing via the function object
        depends on the other functions of the DAG, so could change if we add nodes.

        """
        d = dict()
        for func_node in self.func_nodes:
            d[func_node.out] = func_node
            d[func_node.name] = func_node

            try:
                if func_node.func not in d:
                    # if .func not in d already, remember the link
                    d[func_node.func] = func_node
                else:
                    # if .func was already in there, mark it for removal
                    # (but leaving the key present so that we know about the duplication)
                    d[func_node.func] = None
            except TypeError:
                # ignore (and don't include func) if not hashable
                pass

        # remove the items marked for removal and return
        return {k: v for k, v in d.items() if v is not None}

    def find_func_node(self, node, default=None):
        if isinstance(node, FuncNode):
            return node
        return self._func_node_for.get(node, default)

    def __iter__(self):
        """Yields the self.func_nodes
        Note: The raison d'etre of this ``__iter__`` is simply because if no custom one
        is provided, python defaults to yielding ``__getitem__[i]`` for integers,
        which leads to an error being raised.

        At least here we yield something sensible.

        A consequence of the `__iter__` being the iterable of func_nodes is that we
        can extend dags using the star operator. Consider the following dag;

        >>> def f(a): return a * 2
        >>> def g(f, b): return f + b
        >>> dag = DAG([f, g])
        >>> assert dag(2, 3) == 7

        Say you wanted to now take a, b, and the output og g, and feed it to another
        function...

        >>> def h(a, b, g): return f"{a=} {b=} {g=}"
        >>> extended_dag = DAG([*dag, h])
        >>> extended_dag(a=2, b=3)
        'a=2 b=3 g=7'
        """
        yield from self.func_nodes

    # ------------ utils --------------------------------------------------------------

    @property
    def params_for_src(self):
        """The ``{src_name: list_of_params_using_that_src,...}`` dictionary.
        That is, a ``dict`` having lists of all ``Parameter`` objs that are used by a
        ``node.bind`` source (value of ``node.bind``) for each such source in the graph

        For each ``func_node``, ``func_node.bind`` gives us the
        ``{param: varnode_src_name}`` specification that tells us where (key of scope)
        to source the arguments of the ``func_node.func`` for each ``param`` of that
        function.

        What ``params_for_src`` is, is the corresponding inverse map.
        The ``{varnode_src_name: list_of_params}`` gathered by scanning each
        ``func_node`` of the DAG.
        """
        d = defaultdict(list)
        for node in self.func_nodes:
            for arg_name, src_name in node.bind.items():
                d[src_name].append(node.sig.parameters[arg_name])
        return dict(d)

    def src_name_params(self, src_names: Optional[Iterable[str]] = None):
        """Generate Parameter instances that are needed to compute ``src_names``"""
        # see params_for_src property to see what d is
        d = self.params_for_src
        if src_names is None:  # if no src_names given, use the names of all var_nodes
            src_names = set(d)

        # For every src_name of the DAG that is in ``src_names``...
        for src_name in filter(src_names.__contains__, d):
            params = d[src_name]  # consider all the params that use it
            # make version of these params that have the same name (namely src_name)
            params_with_name_changed_to_src_name = [
                p.replace(name=src_name) for p in params
            ]
            if len(params_with_name_changed_to_src_name) == 1:
                # if there's only one param, yield it (there can be no conflict)
                yield params_with_name_changed_to_src_name[0]
            else:  # if there's more than one param, merge them
                # How to resolve conflicts (different defaults, annotations or kinds)
                # is determined by what ``parameter_merge`` specified, which is,
                # by default, strict (everything needs to be the same, or
                # ``parameter_merge`` with raise an error.)
                yield self.parameter_merge(params_with_name_changed_to_src_name)

    # TODO: Find more representative (and possibly shorter) doctest:
    @property
    def graph_ids(self):
        """The dict representing the ``{from_node: to_nodes}`` graph.
        Like ``.graph``, but with node ids (names).

        >>> from meshed.dag import DAG
        >>> def add(a, b=1): return a + b
        >>> def mult(x, y=3): return x * y
        >>> def exp(mult, a): return mult ** a
        >>> assert DAG([add, mult, exp]).graph_ids == {
        ...     'a': ['add_', 'exp_'],
        ...     'b': ['add_'],
        ...     'add_': ['add'],
        ...     'x': ['mult_'],
        ...     'y': ['mult_'],
        ...     'mult_': ['mult'],
        ...     'mult': ['exp_'],
        ...     'exp_': ['exp']
        ... }

        """
        return {
            _name_attr_or_x(k): list(map(_name_attr_or_x, v))
            for k, v in self.graph.items()
        }

    def __add__(self, other):
        """A union of DAGs.

        :param other: Another DAG or a valid object to make one with ``DAG(other)``.

        >>> dag = DAG(list) + DAG(tuple)
        >>> print(dag.synopsis_string(bind_info='hybrid'))
        iterable -> tuple_ -> tuple
        iterable -> list_ -> list
        >>> dag([1,2,3])
        ((1, 2, 3), [1, 2, 3])
        """
        if isinstance(other, DAG):
            other = list(other.func_nodes)
        else:
            other = list(DAG(other).func_nodes)
        return DAG(list(self.func_nodes) + other)

    def copy(self, renamer=numbered_suffix_renamer):
        return DAG(ch_names(self.func_nodes, renamer=renamer))

    def add_edge(self, from_node, to_node, to_param=None):
        """Add an e

        :param from_node:
        :param to_node:
        :param to_param:
        :return: A new DAG with the edge added

        >>> def f(a, b): return a + b
        >>> def g(c, d=1): return c * d
        >>> def h(x, y=1): return x ** y
        >>>
        >>> three_funcs = DAG([f, g, h])
        >>> assert (
        ...     three_funcs(x=1, c=2, a=3, b=4)
        ...     == (1, 2, 7)
        ...     == (h(x=1), g(c=2), f(a=3, b=4))
        ...     == (1 ** 1, 2 * 1, 3 + 4)
        ... )
        >>> print(three_funcs.synopsis_string())
        x,y -> h_ -> h
        c,d -> g_ -> g
        a,b -> f_ -> f
        >>> hg = three_funcs.add_edge('h', 'g')
        >>> assert (
        ...     hg(a=3, b=4, x=1)
        ...     == (7, 1)
        ...     == (f(a=3, b=4), g(c=h(x=1)))
        ...     == (3 + 4, 1 * (1 ** 1))
        ... )
        >>> print(hg.synopsis_string())
        a,b -> f_ -> f
        x,y -> h_ -> h
        h,d -> g_ -> g
        >>>
        >>> fhg = three_funcs.add_edge('h', 'g').add_edge('f', 'h')
        >>> assert (
        ...     fhg(a=3, b=4)
        ...     == 7
        ...     == g(h(f(3, 4)))
        ...     == ((3 + 4) * 1) ** 1
        ... )
        >>> print(fhg.synopsis_string())
        a,b -> f_ -> f
        f,y -> h_ -> h
        h,d -> g_ -> g

        The from and to nodes can be expressed by the ``FuncNode`` ``name`` (identifier)
        or ``out``, or even the function itself if it's used only once in the ``DAG``.

        >>> fhg = three_funcs.add_edge(h, 'g').add_edge('f_', 'h')
        >>> assert fhg(a=3, b=4) == 7

        By default, the edge will be added from ``from_node.out`` to the first
        parameter of the function of ``to_node``.
        But if you want otherwise, you can specify the parameter the edge should be
        connected to.
        For example, see below how we connect the outputs of ``g`` and ``h`` to the
        parameters ``a`` and ``b`` of ``f`` respectively:

        >>> f_of_g_and_h = (
        ...     DAG([f, g, h])
        ...     .add_edge(g, f, to_param='a')
        ...     .add_edge(h, f, 'b')
        ... )
        >>> assert (
        ...     f_of_g_and_h(x=2, c=3, y=2, d=2)
        ...     == 10
        ...     == f(g(c=3, d=2), h(x=2, y=2))
        ...     == 3 * 2 + 2 ** 2
        ... )
        >>>
        >>> print(f_of_g_and_h.synopsis_string())
        x,y -> h_ -> h
        c,d -> g_ -> g
        g,h -> f_ -> f

        See Also ``DAG.add_edges`` to add multiple edges at once

        """
        # resolve from_node and to_node into FuncNodes
        from_node, to_node = map(self.find_func_node, (from_node, to_node))
        if to_node is None and callable(to_node):
            to_node = FuncNode(
                to_node
            )  # TODO: Automatically avoid clashing with dag identifiers (?)

        # if to_param is None, take the first parameter of to_node as the one
        if to_param is None:
            if not to_node.bind:
                raise InvalidFunctionParameters(
                    "You can't add an edge TO a FuncNode whose function has no "
                    'parameters. '
                    f'You attempted to add an edge between {from_node=} and {to_node=}.'
                )
            else:
                # first param of .func (i.e. first key of .bind)
                to_param = next(iter(to_node.bind))

        existing_bind = to_node.bind[to_param]
        if any(existing_bind == fn.out for fn in self.func_nodes):
            raise ValueError(
                f"The {to_node} node is already sourcing '{to_param}' from '"
                f"{existing_bind}'."
                'Delete that edge to be able before you add a new one'
            )

        new_to_node_dict = to_node.to_dict()
        new_bind = new_to_node_dict['bind'].copy()
        new_bind[to_param] = from_node.out  # this is the actual edge creation
        new_to_node = FuncNode.from_dict(dict(new_to_node_dict, bind=new_bind))
        return DAG(
            replace_item_in_iterable(
                self.func_nodes,
                condition=lambda x: x == to_node,
                replacement=lambda x: new_to_node,
            )
        )

    # TODO: There are optimization and pre-validation opportunities here!
    def add_edges(self, edges):
        """Adds multiple edges by applying ``DAG.add_edge`` multiple times.

        :param edges: An iterable of ``(from_node, to_node)`` pairs or
            ``(from_node, to_node, param)`` triples.
        :return: A new dag with the said edges added.

        >>> def f(a, b): return a + b
        >>> def g(c, d=1): return c * d
        >>> def h(x, y=1): return x ** y
        >>> fhg = DAG([f, g, h]).add_edges([(h, 'g'), ('f_', 'h')])
        >>> assert fhg(a=3, b=4) == 7
        """
        dag = self
        for edge in edges:
            dag = dag.add_edge(*edge)
        return dag

    # ------------ display --------------------------------------------------------------

    def synopsis_string(self, bind_info='values'):
        return '\n'.join(
            func_node.synopsis_string(bind_info) for func_node in self.func_nodes
        )

    # TODO: Give more control (merge with lined)
    def dot_digraph_body(self, start_lines=()):
        """Make lines for dot (graphviz) specification of DAG

        >>> def add(a, b=1): return a + b
        >>> def mult(x, y=3): return x * y
        >>> def exp(mult, a): return mult ** a
        >>> func_nodes = [
        ...     FuncNode(add, out='x'), FuncNode(mult, name='the_product'), FuncNode(exp)
        ... ]

        #
        # >>> assert list(DAG(func_nodes).dot_digraph_body()) == [
        # ]
        """
        if isinstance(start_lines, str):
            start_lines = start_lines.split()  # TODO: really? split on space?
        yield from dot_lines_of_func_nodes(self.func_nodes, start_lines=start_lines)

    @wraps(dot_digraph_body)
    def dot_digraph_ascii(self, *args, **kwargs):
        """Get an ascii art string that represents the pipeline"""
        from meshed.util import dot_to_ascii

        return dot_to_ascii('\n'.join(self.dot_digraph_body(*args, **kwargs)))

    @wraps(dot_digraph_body)
    def dot_digraph(self, *args, **kwargs):
        try:
            import graphviz
        except (ModuleNotFoundError, ImportError) as e:
            raise ModuleNotFoundError(
                f'{e}\nYou may not have graphviz installed. '
                f'See https://pypi.org/project/graphviz/.'
            )
        # Note: Since graphviz 0.18, need to have a newline in body lines!
        body = list(map(_add_new_line_if_none, self.dot_digraph_body(*args, **kwargs)))
        return graphviz.Digraph(body=body)

    @property
    def __name__(self):
        return self.name or 'DAG'

    # NOTE: "sig = property(__signature__)" is not working. So, doing the following instead.
    @property
    def sig(self):
        return self.__signature__

    @sig.setter
    def sig(self, value):
        self.__signature__ = value


# These are the defaults used in lined.
# TODO: Merge some of the functionalities around graph displays in lined and meshed
dflt_configs = dict(
    fnode_shape='box',
    vnode_shape='none',
    display_all_arguments=True,
    edge_kind='to_args_on_edge',
    input_node=True,
    output_node='output',
)


def param_to_dot_definition(p: Parameter, shape=dflt_configs['vnode_shape']):
    if p.default is not empty:
        name = p.name + '='
    elif p.kind == p.VAR_POSITIONAL:
        name = '*' + p.name
    elif p.kind == p.VAR_KEYWORD:
        name = '**' + p.name
    else:
        name = p.name
    yield f'{p.name} [label="{name}" shape="{shape}"]'


def call_func(func, kwargs):
    kwargs = {k.__name__: v for k, v in kwargs.items()}
    return Sig(func).source_kwargs(kwargs)


def dot_lines_of_func_parameters(
    parameters: Iterable[Parameter],
    out: str,
    func_id: str,
    *,
    func_label: str = None,
    output_shape: str = dflt_configs['vnode_shape'],
    func_shape: str = dflt_configs['fnode_shape'],
) -> Iterable[str]:
    assert func_id != out, (
        f"Your func and output name shouldn't be the " f'same: {out=} {func_id=}'
    )
    func_label = func_label or func_id
    yield f'{out} [label="{out}" shape="{output_shape}"]'
    yield f'{func_id} [label="{func_label}" shape="{func_shape}"]'
    yield f'{func_id} -> {out}'
    # args -> func
    for p in parameters:
        yield from param_to_dot_definition(p)
    for p in parameters:
        yield f'{p.name} -> {func_id}'


def _parameters_and_names_from_sig(
    sig: Sig, out=None, func_name=None,
):
    func_name = func_name or sig.name
    out = out or sig.name
    if func_name == out:
        func_name = '_' + func_name
    assert isinstance(func_name, str) and isinstance(out, str)
    return sig.parameters, out, func_name


def dot_lines_of_func_nodes(func_nodes: Iterable[FuncNode], start_lines=()):
    r"""Got lines generator for the graphviz.DiGraph(body=list(...))

    >>> def add(a, b=1):
    ...     return a + b
    >>> def mult(x, y=3):
    ...     return x * y
    >>> def exp(mult, a):
    ...     return mult ** a
    >>> func_nodes = [
    ...     FuncNode(add, out='x'),
    ...     FuncNode(mult, name='the_product'),
    ...     FuncNode(exp)
    ... ]
    >>> lines = list(dot_lines_of_func_nodes(func_nodes))
    >>> assert lines == [
    ... 'x [label="x" shape="none"]',
    ... '_add [label="_add" shape="box"]',
    ... '_add -> x',
    ... 'a [label="a" shape="none"]',
    ... 'b [label="b=" shape="none"]',
    ... 'a -> _add',
    ... 'b -> _add',
    ... 'mult [label="mult" shape="none"]',
    ... 'the_product [label="the_product" shape="box"]',
    ... 'the_product -> mult',
    ... 'x [label="x" shape="none"]',
    ... 'y [label="y=" shape="none"]',
    ... 'x -> the_product',
    ... 'y -> the_product',
    ... 'exp [label="exp" shape="none"]',
    ... '_exp [label="_exp" shape="box"]',
    ... '_exp -> exp',
    ... 'mult [label="mult" shape="none"]',
    ... 'a [label="a" shape="none"]',
    ... 'mult -> _exp',
    ... 'a -> _exp'
    ... ]  # doctest: +SKIP

    >>> from meshed.util import dot_to_ascii
    >>>
    >>> print(dot_to_ascii('\n'.join(lines)))  # doctest: +SKIP
    <BLANKLINE>
                    a        ─┐
                              │
               │              │
               │              │
               ▼              │
             ┌─────────────┐  │
     b=  ──▶ │    _add     │  │
             └─────────────┘  │
               │              │
               │              │
               ▼              │
                              │
                    x         │
                              │
               │              │
               │              │
               ▼              │
             ┌─────────────┐  │
     y=  ──▶ │ the_product │  │
             └─────────────┘  │
               │              │
               │              │
               ▼              │
                              │
                  mult        │
                              │
               │              │
               │              │
               ▼              │
             ┌─────────────┐  │
             │    _exp     │ ◀┘
             └─────────────┘
               │
               │
               ▼
    <BLANKLINE>
                   exp
    <BLANKLINE>

    """
    yield from start_lines
    validate_that_func_node_names_are_sane(func_nodes)
    for func_node in func_nodes:
        yield from dot_lines_of_func_node(func_node)


def dot_lines_of_func_node(func_node: FuncNode):

    out = func_node.out
    func_id = func_node.name
    func_label = getattr(func_node, 'func_label', func_id)
    if out == func_id:  # though forbidden in default FuncNode validation
        func_id = '_' + func_id

    # Get the Parameter objects for sig, with names changed to bind ones
    params = func_node.sig.ch_names(**func_node.bind).params

    yield from dot_lines_of_func_parameters(
        params, out=out, func_id=func_id, func_label=func_label
    )


def _add_new_line_if_none(s: str):
    """Since graphviz 0.18, need to have a newline in body lines.
    This util is there to address that, adding newlines to body lines
    when missing."""
    if s and s[-1] != '\n':
        return s + '\n'
    return s


def print_dag_string(dag: DAG, bind_info: str = 'hybrid'):
    print(dag.synopsis_string(bind_info=bind_info))


# ---------------------------------------------------------------------------------------
# reordering funcnodes

from meshed.util import uncurry, pairs

mk_mock_funcnode_from_tuple = uncurry(mk_mock_funcnode)


def funcnodes_from_pairs(pairs):
    return list(map(mk_mock_funcnode_from_tuple, pairs))


def reorder_on_constraints(funcnodes, outs):
    extra_nodes = funcnodes_from_pairs(pairs(outs))
    funcnodes += extra_nodes
    graph = _func_nodes_to_graph_dict(funcnodes)
    nodes = topological_sort(graph)
    print('after ordering:', nodes)
    ordered_nodes = [node for node in nodes if node not in extra_nodes]
    func_nodes, var_nodes = _separate_func_nodes_and_var_nodes(ordered_nodes)

    return func_nodes, var_nodes


def attribute_vals(objs: Iterable, attrs: Iterable[str], egress=None):
    """Extract attributes from an iterable of objects
    >>> list(attribute_vals([print, map], attrs=['__name__', '__module__']))
    [('print', 'builtins'), ('map', 'builtins')]
    """
    if isinstance(attrs, str):
        attrs = attrs.split()
    val_tuples = map(attrgetter(*attrs), objs)
    if egress:
        return egress(val_tuples)
    else:
        return val_tuples


names_and_outs = partial(attribute_vals, attrs=('name', 'out'), egress=chain)

DagAble = Union[DAG, Iterable[FuncNodeAble]]


def _validate_func_src(func_src, func_nodes: DagAble):
    allowed_identifiers = set(names_and_outs(DAG(func_nodes).func_nodes))
    if not_allowed := (func_src.keys() - allowed_identifiers):
        raise ValueError(
            f"These identifiers weren't found in func_nodes: {not_allowed}"
        )
    if not_callable := set(filter(lambda x: not callable(x), func_src.values())):
        raise ValueError(f"These values of func_src weren't callable: {not_callable}")


# TODO: Include as method of DAG?
# TODO: extract egress functionality to decorator
@double_up_as_factory
def ch_funcs(func_nodes: DagAble = None, *, func_src=(), strict=False):
    if isinstance(func_nodes, DAG):
        egress = DAG
    else:
        egress = list

    func_src = dict(func_src)
    if strict:
        _validate_func_src(func_src, func_nodes)

    def gen():
        for fn in func_nodes:
            if (
                new_func := func_src.get(fn.out, func_src.get(fn.name, None))
            ) is not None:
                new_fn_kwargs = dict(fn.to_dict(), func=new_func)
                yield FuncNode.from_dict(new_fn_kwargs)
            else:
                yield fn

    return egress(gen())


change_funcs = ch_funcs  # back-compatibility


# TODO: Include as method of DAG?
# TODO: extract egress functionality to decorator
@double_up_as_factory
def ch_names(func_nodes: DagAble = None, *, renamer: Renamer = numbered_suffix_renamer):
    """Renames variables and functions of a ``DAG`` or iterable of ``FuncNodes``.

    :param func_nodes: A ``DAG`` of iterable of ``FuncNodes``
    :param renamer: A function taking an old name and returning the new one, or:
        - A dictionary ``{old_name: new_name, ...}`` mapping old names to new ones
        - A string, which will be appended to all identifiers of the ``func_nodes``
    :return: func_nodes with some or all identifiers changed. If the input ``func_nodes``
    is an iterable of ``FuncNodes``, a list of func_nodes will be returned, and if the
    input ``func_nodes`` is a ``DAG`` instance, a ``DAG`` will be returned.

    >>> from meshed.makers import code_to_dag
    >>> from meshed.dag import print_dag_string
    >>>
    >>> @code_to_dag
    ... def dag():
    ...     b = f(a)
    ...     c = g(x=a)
    ...     d = h(b, y=c)


    This is what the dag looks like:

    >>> print_dag_string(dag)
    x=a -> g -> c
    a -> f -> b
    b,y=c -> h -> d

    Now, if rename the vars of the ``dag`` without further specifying how, all of our
    nodes (names) will be suffixed with a ``_1``

    >>> new_dag = ch_names(dag)
    >>> print_dag_string(new_dag)
    a=a_1 -> f_1 -> b_1
    x=a_1 -> g_1 -> c_1
    b=b_1,y=c_1 -> h_1 -> d_1

    If any nodes are already suffixed by ``_`` followed by a number, the default
    renamer (``numbered_suffix_renamer``) will increment that number:

    >>> another_new_data = ch_names(new_dag)
    >>> print_dag_string(another_new_data)
    x=a_2 -> g_2 -> c_2
    a=a_2 -> f_2 -> b_2
    b=b_2,y=c_2 -> h_2 -> d_2

    If we specify a string for the ``renamer`` argument, it will be used to suffix all
    the nodes.

    >>> print_dag_string(ch_names(dag, renamer='_copy'))
    a=a_copy -> f_copy -> b_copy
    x=a_copy -> g_copy -> c_copy
    b=b_copy,y=c_copy -> h_copy -> d_copy

    Finally, for full functionality on renaming, you can use a function

    >>> print_dag_string(ch_names(dag, renamer=lambda x: f"{x.upper()}"))
    a=A -> F -> B
    x=A -> G -> C
    b=B,y=C -> H -> D

    In all the above our input was a ``DAG`` so we got a ``DAG`` back, but if we enter
    an iterable of ``FuncNode`` instances, we'll get a list of the same back.
    Also, know that if your function returns ``None`` for a given identifier, it will
    have the effect of not changing that identifier.

    >>> ch_names(dag.func_nodes, renamer=lambda x: x.upper() if x in 'abc' else None)
    [FuncNode(x=A -> g -> C), FuncNode(a=A -> f -> B), FuncNode(b=B,y=C -> h -> d)]

    If you want to rename the nodes with an explicit mapping, you can do so by
    specifying this mapping as your renamer

    >>> substitutions = {'a': 'alpha', 'b': 'bravo'}
    >>> print_dag_string(ch_names(dag, renamer=substitutions))
    a=alpha -> f -> bravo
    x=alpha -> g -> c
    b=bravo,y=c -> h -> d

    """
    if isinstance(func_nodes, DAG):
        egress = DAG
    else:
        egress = list
    renamer = renamer or numbered_suffix_renamer
    if isinstance(renamer, str):
        suffix = renamer
        renamer = lambda name: f'{name}{suffix}'
    elif isinstance(renamer, Mapping):
        old_to_new_map = dict(renamer)
        renamer = old_to_new_map.get
    assert callable(renamer), f'Could not be resolved into a callable: {renamer}'
    ktrans = partial(_rename_node, renamer=renamer)
    func_node_trans = partial(func_node_transformer, kwargs_transformers=ktrans)
    return egress(map(func_node_trans, func_nodes))


def _rename_node(fn_kwargs, renamer: Renamer = numbered_suffix_renamer):
    fn_kwargs = fn_kwargs.copy()
    # decorate renamer so if the original returns None the decorated will return input
    renamer = _if_none_return_input(renamer)
    fn_kwargs['name'] = renamer(fn_kwargs['name'])
    fn_kwargs['out'] = renamer(fn_kwargs['out'])
    fn_kwargs['bind'] = {
        param: renamer(var_id) for param, var_id in fn_kwargs['bind'].items()
    }
    return fn_kwargs


rename_nodes = ch_names  # back-compatibility
