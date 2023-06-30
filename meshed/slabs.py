"""Tools to generate slabs.

A slab is a dict that holds data generated by a stream for a given interval of time.

The main object of this module is `Slabs`, and object that defines how to
generate multiple streams, in the form of slabs.
More precisely, it defines how to source streams, operate on and combine these to create
further streams, and even push these streams to further processes, all through a single
simple interface: An (ordered) list of components that are called in sequence to either
pull data from some sources, compute a new stream based on previous ones, or push some
of the streams to further processes (such as visualization, or storage systems).

A slab is a collection of items of a same interval of time.
We represent a slab using a `dict` or mapping.
Typically, a slab will be the aggregation of multiple information streams that
happened around the same time.

`Slabs` is a tool that allows you to source multiple streams into a stream of
slabs that can contain the original data, or other datas computed from it, or both.

Note to developers, though the code below is a reduced form of the actual code,
it should be enough to understand the general idea.
For a discussion about the design of Slabs, see
https://github.com/i2mint/meshed/discussions/49.


>>> class Slabs:
...     def _call_on_scope(self, scope):
...         '''
...         Calls the components 1 by 1, sourcing inputs and writing outputs in scope
...         '''
...
...     def __next__(self):
...         '''Get the next slab by calling _call_on_scope on an new empty scope.
...         At least one of the components will have to be argument-less and provide
...         some data for other components to get their inputs from, if any are needed.
...         '''
...         return self._call_on_scope(scope={})
...
...     def __iter__(self):
...         '''Iterates over slabs until a handle exception is raised.'''
...         # Simplified code:
...         with self:  # enter all the contexts that need to be entered
...             while True:  # loop until you encounter a handled exception
...                 try:
...                     yield next(self)
...                 except self.handle_exceptions as exc_val:
...                     # use specific exceptions to signal that iteration should stop
...                     break

"""

from typing import (
    Callable,
    Mapping,
    Iterable,
    Union,
    Any,
    MutableMapping,
    Protocol,
)
from i2 import Sig, ContextFanout
from meshed.base import FuncNode
from meshed.dag import DAG


class ExceptionalException(Exception):
    """Raised when an exception was supposed to be handled, but no matching handler
    was found.

    See the `_handle_exception` function, where it is raised.
    """


class IteratorExit(BaseException):
    """Raised when an iterator should quit being iterated on, signaling this event
    any process that cares to catch the signal.
    We chose to inherit directly from `BaseException` instead of `Exception`
    for the same reason that `GeneratorExit` does: Because it's not technically
    an error.

    See: https://docs.python.org/3/library/exceptions.html#GeneratorExit
    """


DFLT_INTERRUPT_EXCEPTIONS = (StopIteration, IteratorExit, KeyboardInterrupt)

DoNotBreak = type("DoNotBreak", (), {})
do_not_break = DoNotBreak()
do_not_break.__doc__ = (
    "Sentinel that should be used to signal Slabs iteration not to break. "
    "This sentinel should be returned by exception handlers if they want to tell "
    "the iteration not to stop (in all other cases, the iteration will stop)"
)

IgnoredOutput = Any
ExceptionHandlerOutput = Union[IgnoredOutput, DoNotBreak]


class ExceptionHandler(Protocol):
    """An exception handler is an argument-less callable that is called when a handled
    exception occurs during iteration. Most often, the handler does nothing,
    but could be used whose output will be ignored, unless it is do_not_break,
    which will signal that the iteration should continue."""

    def __call__(self) -> ExceptionHandlerOutput:
        pass


# TODO: Make HandledExceptionsMap into a NewType?
# doc: A map between exception types and exception handlers (callbacks)
ExceptionType = type(BaseException)
HandledExceptionsMap = Mapping[ExceptionType, ExceptionHandler]

# doc: If none of the exceptions need handlers, you can just specify a list of them
HandledExceptionsMapSpec = Union[
    HandledExceptionsMap,
    Iterable[BaseException],  # an iterable of exception types
    BaseException,  # or just one exception type
]


def do_nothing():
    pass


def log_and_return(msg, logger=print):
    logger(msg)
    return msg


# TODO: Could consider (topologically) ordering the exceptions to reduce the matching
#  possibilities (see _handle_exception)
def _get_handle_exceptions(
    handle_exceptions: HandledExceptionsMapSpec,
) -> HandledExceptionsMap:
    if isinstance(handle_exceptions, BaseException):
        # Only one? Ensure there's a tuple of exceptions:
        handle_exceptions = (handle_exceptions,)
    if not isinstance(handle_exceptions, Mapping):
        handle_exceptions = {exc_type: do_nothing for exc_type in handle_exceptions}
    return handle_exceptions


def _handle_exception(
    instance, exc_val: BaseException, handle_exceptions: HandledExceptionsMap
) -> ExceptionHandlerOutput:
    """Looks for an exception type matching exc_val and calls the corresponding
    handler with
    """
    inputs = dict(exc_val=exc_val, instance=instance)
    if type(exc_val) in handle_exceptions:  # try precise matching first
        exception_handler = handle_exceptions[type(exc_val)]
        return _call_from_dict(inputs, exception_handler, Sig(exception_handler))

    else:  # if not, find the first matching parent
        for exc_type, exception_handler in handle_exceptions.items():
            if isinstance(exc_val, exc_type):
                return _call_from_dict(
                    inputs, exception_handler, Sig(exception_handler)
                )
    # You never should get this far, but if you do, there's a problem, let's scream it:
    raise ExceptionalException(
        f"I couldn't find that exception in my handlers: {exc_val}"
    )


def _call_from_dict(kwargs: MutableMapping, func: Callable, sig: Sig):
    """A i2.call_forgivingly optimized for our purpose

    The sig argument needs to be the Sig(func) to work correctly.

    Two uses cases here:

    - using a scope dict as both the source of `Slabs` components, and as a place
    to temporarily store the outputs of these components.

    - exception handlers: We'd like the exception handlers to be easy to express.
    Maybe you need the object raising the exception to handle it,
    maybe you just want to log the event.
    In the first case, you the handler needs the said object to be passed to it,
    in the second, we don't need any arguments at all.
    With _call_from_dict, we don't have to choose, we just have to impose that
    the handler use specific keywords (namely `exc_val` and/or `instance`)
    when there are inputs.

    """
    args, kwargs = sig.args_and_kwargs_from_kwargs(
        kwargs,
        allow_excess=True,
        ignore_kind=True,
        allow_partial=False,
        apply_defaults=True,
    )
    return func(*args, **kwargs)


def _conditional_pluralization(n_items, singular_msg, plural_msg):
    """To route to the right message (or template) according to ``n_items``"""
    if n_items == 1:
        return singular_msg
    else:
        return plural_msg


def _validate_components(components):
    if not all(map(callable, components.values())):
        not_callable = [k for k, v in components.items() if not callable(v)]
        not_callable_keys = ", ".join(not_callable)
        # TODO: Analyze values of components further and enhance error message with
        #  further suggestions. For example, if there's an iterator component c,
        #  suggest that perhaps ``c.__next__`` was intended?
        #  These component-based suggestions should be placed as a default of an
        #  argument of _validate_components so that it can be parametrized.
        msg = _conditional_pluralization(
            len(not_callable),
            f"This component is not callable: {not_callable_keys}",
            f"These components are not callable: {not_callable_keys}",
        )
        raise TypeError(msg)


# TODO: Postelize (or add tooling for) the components specification and add validation.
class Slabs:
    """Object to source and manipulate multiple streams.

    A slab is a collection of items of a same interval of time.
    We represent a slab using a `dict` or mapping.
    Typically, a slab will be the aggregation of multiple information streams that
    happened around the same time.

    For example, say and edge device had a microphone, light, and movement sensor.
    An aggregate reading of these sensors could give you something like:

    >>> slab = {'audio': [1, 2, 4], 'light': 126, 'movement': None}

    `movement` is `None` because the sensor is off. If it were on, we'd have True or
    False as values.

    From this information, you'd like to compute a `turn_mov_on` value based on the
    formula.

    >>> from statistics import stdev
    >>> vol = stdev
    >>> should_turn_movement_sensor_on = lambda audio, light: vol(audio) * light > 50000

    The produce of the volume and the lumens gives you 192, so you now have...

    >>> slab = {
    ...     'audio': [1, 2, 4],
    ...     'light': 126,
    ...     'should_turn_movement_sensor_on': False,
    ...     'movement': None
    ... }

    The next slab that comes in is

    >>> slab = {'audio': [-96, 89, -92], 'light': 501, 'movement': None}

    which puts us over the threshold so

    >>> slab = {
    ...     'audio': [-96, 89, -92],
    ...     'light': 501,
    ...     'should_turn_movement_sensor_on': True,
    ...     'movement': None
    ... }

    and the movement sensor is turned on, the movement is detected, a `human_presence`
    signal is computed, and a notification sent if that metric is above a given theshold.

    The point here is that we incrementally compute various fields, enhancing our slab
    of information, and we do so iteratively over over slab that is streaming to us
    from our smart home device.

    `SlabsIter` is there to help you create such slabs, from source to enhanced.

    The situation above would look something along like this:

    >>> from statistics import stdev
    >>>
    >>> vol = stdev
    >>>
    >>> # Making a slabs iter object
    >>> def make_a_slabs_iter():
    ...
    ...     # Mocking the sensor readers
    ...     audio_sensor_read = iter([[1, 2, 3], [-96, 87, -92], [320, -96, 99]]).__next__
    ...     light_sensor_read = iter([126, 501, 523]).__next__
    ...     movement_sensor_read = iter([None, None, True]).__next__
    ...
    ...     return Slabs(
    ...         # The first three components get data from the sensors.
    ...         # The *_read objects are all callable, returning the next
    ...         # chunk of data for that sensor, if any.
    ...         audio=audio_sensor_read,
    ...         light=light_sensor_read,
    ...         movement=movement_sensor_read,
    ...         # The next
    ...         should_turn_movement_sensor_on = lambda audio, light: vol(audio) * light > 50000,
    ...         human_presence_score = lambda audio, light, movement: movement and sum([vol(audio), light]),
    ...         should_notify = lambda human_presence_score: human_presence_score and human_presence_score > 700,
    ...         notify = lambda should_notify: print('someone is there') if should_notify else None
    ...     )
    ...
    >>>
    >>> si = make_a_slabs_iter()
    >>> next(si)  # doctest: +NORMALIZE_WHITESPACE
    {'audio': [1, 2, 3],
     'light': 126,
     'movement': None,
     'should_turn_movement_sensor_on': False,
     'human_presence_score': None,
     'should_notify': None,
     'notify': None}
    >>> next(si)  # doctest: +NORMALIZE_WHITESPACE
    {'audio': [-96, 87, -92],
     'light': 501,
     'movement': None,
     'should_turn_movement_sensor_on': True,
     'human_presence_score': None,
     'should_notify': None,
     'notify': None}
    >>> next(si)  # doctest: +NORMALIZE_WHITESPACE
    someone is there
    {'audio': [320, -96, 99],
     'light': 523,
     'movement': True,
     'should_turn_movement_sensor_on': True,
     'human_presence_score': 731.1353726143957,
     'should_notify': True,
     'notify': None}

    If you ask for the next slab, you'll get a `StopIteration` (raised by the mocked
    sources since they reached the end of their iterators).

    >>> next(si)  # doctest: +NORMALIZE_WHITESPACE
    Traceback (most recent call last):
      ...
    StopIteration

    That said, if you iterate through a `SlabsIter` that handles the `StopIteration`
    exception (it does by default), you'll reach the end of you iteration gracefully.

    >>> si = make_a_slabs_iter()
    >>> for slab in si:
    ...     pass
    someone is there
    >>> si = make_a_slabs_iter()
    >>> slabs = list(si)  # gather all the slabs
    someone is there
    >>> len(slabs)
    3
    >>> slabs[-1]  # doctest: +NORMALIZE_WHITESPACE
    {'audio': [320, -96, 99],
     'light': 523,
     'movement': True,
     'should_turn_movement_sensor_on': True,
     'human_presence_score': 731.1353726143957,
     'should_notify': True,
     'notify': None}

    Note that ``Slabs`` uses a "scope" to store the intermediate results of the
    computation. This scope is a `dict` by default, but you can pass any
    ``MutableMapping`` to the ``scope_factory`` argument. This means that you can use
    other means to store intermediate results simply by wrapping them in a
    MutableMapping. For example, you could use message broker such as Redis to
    store the intermediate results, and have the components read and write to it.

    To help you with this, check out the `dol <https://pypi.org/project/dol/>`_
    and `py2store <https://pypi.org/project/py2store/>`_ libraries.

    """

    _output_of_context_enter = None

    def __init__(
        self,
        handle_exceptions: HandledExceptionsMapSpec = DFLT_INTERRUPT_EXCEPTIONS,
        scope_factory: Callable[[], MutableMapping] = dict,
        **components,
    ):
        _validate_components(components)
        self.components = components
        self.handle_exceptions = _get_handle_exceptions(handle_exceptions)
        self.scope_factory = scope_factory
        self._handled_exception_types = tuple(self.handle_exceptions)
        self.sigs = {
            name: Sig.sig_or_default(func) for name, func in self.components.items()
        }
        self.context = ContextFanout(**components)

    def _call_on_scope(self, scope: MutableMapping):
        """Calls the components 1 by 1, sourcing inputs and writing outputs in scope"""
        # for each component
        for name, component in self.components.items():
            # call the component using scope to source any arguments it needs
            # and write the result in scope, under the component's name.
            scope[name] = _call_from_dict(scope, component, self.sigs[name])
        return scope

    def __next__(self):
        """Get the next slab by calling _call_on_scope on an new empty scope.
        At least one of the components will have to be argument-less and provide
        some data for other components to get their inputs from, if any are needed.
        """
        return self._call_on_scope(scope=self.scope_factory())

    # TODO: Extend the flow control capabilities of execption handling
    #   (see https://github.com/i2mint/meshed/discussions/49)
    def __iter__(self):
        """Iterates over slabs until a handle exception is raised."""
        with self:  # enter all the contexts that need to be entered
            while True:  # loop until you encounter a handled exception
                try:
                    yield next(self)
                except self._handled_exception_types as exc_val:
                    handler_output = _handle_exception(
                        self, exc_val, self.handle_exceptions
                    )
                    # break, unless the handler tells us not to
                    if handler_output is not do_not_break:
                        self.exit_value = handler_output  # remember, in case useful
                        break

    def open(self):
        self._output_of_context_enter = self.context.__enter__()
        return self

    def close(self, exc_type=None, exc_val=None, exc_tb=None) -> None:
        return self._output_of_context_enter.__exit__(exc_type, exc_val, exc_tb)

    def run(self):
        for _ in self:
            pass

    __enter__ = open
    __exit__ = close
    __call__ = run

    @classmethod
    def from_func_nodes(
        cls,
        func_nodes: Iterable[FuncNode],
        *,
        handle_exceptions: HandledExceptionsMapSpec = DFLT_INTERRUPT_EXCEPTIONS,
        scope_factory: Callable[[], MutableMapping] = dict,
    ):
        """Make"""
        func_nodes = list(func_nodes)
        assert all(
            list(fn.bind.keys()) == list(fn.bind.values()) for fn in func_nodes
        ), (
            "You can't use `from_func_nodes` (yet) your binds are not trivial. "
            "That is, if any of your functions' arguments have different names than "
            "the var nodes they're bound to"
        )
        # TODO: Make it work for non-trivial binds by using i2.wrapper
        components = {n.out: n.func for n in func_nodes}
        return cls(
            handle_exceptions=handle_exceptions,
            scope_factory=scope_factory,
            **components,
        )

    def to_func_nodes(self) -> Iterable[FuncNode]:
        for name, func in self.components.items():
            yield FuncNode(func, name=name, out=name)

    def to_dag(self) -> DAG:
        def use_keys_as_func_names(components):
            for k, func in components.items():
                yield FuncNode(func, name=k, out=k)

        return DAG(list(self.to_func_nodes()))

    # TODO: Add @wraps(dot_digraph_body) to have DAG.dot_digraph signature
    def dot_digraph(self, *args, **kwargs):
        """
        Returns a dot_digraph of the DAG of the SlabsIter (see ``DAG.dot_digraph``)
        """
        dag = self.to_dag()
        return dag.dot_digraph()


SlabsIter = Slabs  # for backward compatibility
