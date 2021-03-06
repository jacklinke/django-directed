import logging

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import models
from django.db.models import Case, When
from django.utils.translation import gettext_lazy as _

from django_directed.context_managers import get_current_graph_instance
from django_directed.models.abstract_base_models import BaseEdge, BaseGraph, BaseNode
from django_directed.query_utils import _ordered_filter

logger = logging.getLogger("django_directed")


def get_model_class(model_fullname: str) -> models.Model:
    """
    Provided with a model fullname (`app_name.ModelName`), Returns
        the associated model class
    """
    split_names = model_fullname.split(".")
    if not len(split_names) == 2:
        raise ImproperlyConfigured("Model fullnames in graph config must be specified as 'app_name.ModeName'")

    app_name, model_name = split_names

    try:
        model_class = apps.get_model(app_name, model_name)
    except (LookupError, ValueError) as e:
        raise ImproperlyConfigured(e)

    return model_class


def get_graph_aware_queryset(config):
    class GraphAwareQuerySet(models.QuerySet):
        def bulk_create(self, objs, batch_size=None, ignore_conflicts=False):
            objs = list(objs)
            for obj in objs:
                obj.provider = get_current_graph_instance(graph_fullname=config.graph_fullname)

            super().bulk_create(objs, batch_size, ignore_conflicts)

    return GraphAwareQuerySet


def get_graph_aware_manager(config):
    class GraphAwareManager(models.Manager):
        pass
        # def get_queryset(self):
        #     graph = get_current_graph_instance(graph_fullname=config.graph_fullname)
        #     queryset = super().get_queryset().filter(graph=graph)
        #     return queryset

    return GraphAwareManager


def base_graph(config):
    class AbstractGraph(BaseGraph):
        """
        Creates "Abstract Graph Model"
        """

        class Meta:
            abstract = True

        def save(self, *args, **kwargs):
            super().save(*args, **kwargs)

        def clean_fields(self, exclude=None):
            super().clean_fields(exclude=exclude)

        def clean(self):
            super().clean()

    return AbstractGraph


def base_edge(config):
    class AbstractEdge(BaseEdge):
        """
        Creates "Abstract Edge Model"
        """

        graph = config.edge_graph_fk_field(
            to=config.graph_fullname,
            null=True,
            related_name="graph_edges",
            related_query_name="graph_edges",
            # related_name="%(app_label)s_%(class)s_related",
            # related_query_name="%(app_label)s_%(class)ss",
            graph_fullname=config.graph_fullname,
        )

        parent = config.edge_parent_fk_field(
            config.node_fullname,
            related_name="child_edges",
            on_delete=models.SET_NULL,
            null=True,
        )
        child = config.edge_child_fk_field(
            config.node_fullname,
            related_name="parent_edges",
            on_delete=models.SET_NULL,
            null=True,
        )

        GraphAwareManager = get_graph_aware_manager(config)
        GraphAwareQuerySet = get_graph_aware_queryset(config)
        CombinedGraphManager = GraphAwareManager.from_queryset(GraphAwareQuerySet)
        objects = CombinedGraphManager()

        class Meta:
            abstract = True

        def save(self, *args, **kwargs):
            # Check for duplicate edges, if needed
            allow_duplicate_edges = config.allow_duplicate_edges
            if not allow_duplicate_edges:
                self.parent.__class__.duplicate_edge_check(self.parent, self.child)

            self.parent.__class__.children_quantity_check(self.parent)  # ToDo: Needs fixing
            super().save(*args, **kwargs)

        def clean_fields(self, exclude=None):
            super().clean_fields(exclude=exclude)

        def clean(self):
            super().clean()

    return AbstractEdge


def base_node(config):
    class AbstractNode(BaseNode):
        """
        Creates "Abstract Node Model"
        """

        def node_class(self):
            return get_model_class(config.node_fullname)

        def edge_class(self):
            return get_model_class(config.edge_fullname)

        def node_table(self):
            return self.node_class()._meta.db_table

        def edge_table(self):
            return self.edge_class()._meta.db_table

        children_blank_null = config.children_blank_null

        GraphAwareManager = get_graph_aware_manager(config)
        GraphAwareQuerySet = get_graph_aware_queryset(config)
        CombinedGraphManager = GraphAwareManager.from_queryset(GraphAwareQuerySet)
        objects = CombinedGraphManager()

        children = config.node_children_m2m_field(
            "self",
            blank=children_blank_null,
            symmetrical=False,
            through=config.edge_fullname,
            through_fields=(  # ToDo: Verify this is in the correct order
                "parent",
                "child",
            ),
            related_name="parents",
        )

        def get_foreign_key_field(self, fk_instance=None):
            """
            Provided a model instance, checks if the edge model has a ForeignKey field to the
            model class of that instance, and then returns the associated field name, else None.
            """
            if fk_instance is not None:
                edge_model = self.edge_class()
                for field in edge_model._meta.get_fields():
                    if field.related_model is fk_instance._meta.model:
                        # Return the first field that matches
                        return field.name
            return None

        def get_pk_name(self):
            """
            Sometimes we set a field other than 'pk' for the primary key.
            This method is used to get the correct primary key field name for the
            model so that raw queries return the correct information.
            """
            return self._meta.pk.name

        def get_pk_type(self):
            """
            The pkid class may be set to a non-default type per-model or across the project.
            This method is used to return the postgres type name for the primary key field so
            that raw queries return the correct information.
            """
            django_pk_type = type(self._meta.pk).__name__

            if django_pk_type == "BigAutoField":
                return "bigint"
            elif django_pk_type == "UUIDField":
                return "uuid"
            else:
                return "integer"

        def ordered_queryset_from_pks(self, pks):
            """
            Generates a queryset, based on the current class and ordered by the provided pks
            """
            return _ordered_filter(self.__class__.objects, "pk", pks)

        def add_child(self, child: BaseNode, **kwargs):
            """Provided with a Node instance, attaches that instance as a child to the current Node instance"""
            kwargs.update({"parent": self, "child": child})

            cls = self.children.through(**kwargs)
            cls.save()
            return cls

        def add_children(self, children: models.QuerySet, **kwargs) -> list:
            """
            Provided with a QuerySet of Node instances, attaches those
              instances as children of the current Node instance
            """
            edge_list = []
            for child in children:
                if child is not None:
                    edge_list.append(self.add_child(child))

            return edge_list

        def add_parent(self, parent: BaseNode, **kwargs):
            """
            Provided with a Node instance, attaches that instance
              as a parent to the current Node instance
            """
            return parent.add_child(child=self, **kwargs)

        def add_parents(self, parents: models.QuerySet, **kwargs) -> list:
            """
            Provided with a QuerySet of Node instances, attaches those
              instances as parents of the current Node instance
            """
            edge_list = []
            for parent in parents:
                edge_list.append(parent.add_child(child=self, **kwargs))

            return edge_list

        def remove_child(self, child: BaseNode = None, delete_node: bool = False):
            """
            Removes the edge connecting this node to the child Node specified.
              Optionally deletes the child node as well.
            """
            if child is not None and child in self.children.all():
                self.children.through.objects.filter(parent=self, child=child).delete()
                if delete_node:
                    # Note: Per django docs:
                    # https://docs.djangoproject.com/en/dev/ref/models/instances/#deleting-objects
                    # This only deletes the object in the database; the Python instance will still
                    # exist and will still have data in its fields.
                    child.delete()
                return True
            logger.debug(
                "Argument `child` in `Node.remove_child()` was not provided or was not a child of the current Node."
            )
            return False

        def remove_children(
            self, children: models.QuerySet = None, remove_all: bool = False, delete_nodes: bool = False
        ):
            """
            Removes the edge connecting this node to each child specified, otherwise removes
            the edges connecting to all children. Optionally deletes the child(ren) node(s) as well.
            """
            all_successful = True

            if children is not None:
                for child in children.all():
                    all_successful = all_successful and self.remove_child(child=child, delete_nodes=delete_nodes)
                if not all_successful:
                    logger.debug("One or more children could not be removed")
                    return False
            elif remove_all:
                for child in self.children.all():
                    all_successful = all_successful and self.remove_child(child=child, delete_nodes=delete_nodes)
                if not all_successful:
                    logger.debug("One or more children could not be removed")
                    return False
            else:
                logger.warning(
                    "`Node.remove_children` should receive an argument for `children` or `remove_all`. "
                    "No action taken."
                )

            return True

        # Pulled from django-postgresql-dag (may need to be moved)

        def raw_queryset(self):

            QUERY = """
            WITH RECURSIVE traverse({pk_name}, depth) AS (
                SELECT first.child_id, 1
                    FROM {relationship_table} AS first
                    LEFT OUTER JOIN {relationship_table} AS second
                    ON first.child_id = second.parent_id
                WHERE first.parent_id = {pk}
            UNION
                SELECT DISTINCT child_id, traverse.depth + 1
                    FROM traverse
                    INNER JOIN {relationship_table}
                    ON {relationship_table}.parent_id = traverse.{pk_name}
                WHERE 1=1
            )
            SELECT {pk_name} FROM traverse
            WHERE depth <= {max_depth}
            GROUP BY {pk_name}
            ORDER BY MAX(depth), {pk_name} ASC
            """

            return self.node_class().objects.raw(
                QUERY.format(
                    relationship_table=self.edge_table(),
                    pk_name=self.get_pk_name(),
                    pk=self.pk,
                    max_depth=100,
                ),
            )

        def descendants_raw(self, **kwargs):
            """Returns a raw QuerySet of all nodes in connected paths in a leafward direction"""
            return self.raw_queryset()

        def descendants(self, **kwargs):
            """Returns a QuerySet of all nodes in connected paths in a leafward direction"""
            pks = [item.pk for item in self.descendants_raw(**kwargs)]
            return self.ordered_queryset_from_pks(pks)

        def descendants_count(self):
            """Returns an integer number representing the total number of descendant nodes"""
            return self.descendants().count()

        def self_and_descendants(self, **kwargs):
            """Returns a QuerySet of all nodes in connected paths in a leafward direction, prepending with self"""
            pks = [self.pk] + [item.pk for item in self.descendants_raw(**kwargs)]
            return self.ordered_queryset_from_pks(pks)

        def descendants_and_self(self, **kwargs):
            """Returns a QuerySet of all nodes in connected paths in a leafward direction, appending with self"""
            pks = [item.pk for item in self.descendants_raw(**kwargs)] + [self.pk]
            return self.ordered_queryset_from_pks(pks)

        # Checks

        @staticmethod
        def self_link_check(parent, child):
            """Checks that the Node is not linked to itself"""
            if parent == child:
                raise ValidationError("The object cannot be linked to itself")

        @classmethod
        def circular_check(cls, parent, child):
            """Checks that the Node is not linked to an ancestor"""

            # Whenever we check for circular links, we also check for self-links (which are a type of circular link)
            cls.self_link_check(parent, child)

            if child in parent.self_and_ancestors():
                raise ValidationError("The new child Node is already an ancestor")

        @staticmethod
        def duplicate_edge_check(parent, child):
            """Checks that the Node is not linked in duplicate to another Node"""
            if child in parent.self_and_descendants():
                raise ValidationError("The new Edge is a duplicate")

        @staticmethod
        def children_quantity_check(parent):
            """Checks that the Node has no more than the allowed number of children, if specified"""
            children_quantity_max = (
                config.children_quantity_max
                if config.children_quantity_max and config.children_quantity_max > 0
                else False
            )
            if children_quantity_max and parent.children.all().count() >= children_quantity_max:
                raise ValidationError("The maximum number of children per node will be exceeded")

        class Meta:
            abstract = True

        def save(self, *args, **kwargs):
            super().save(*args, **kwargs)

        def clean_fields(self, exclude=None):
            super().clean_fields(exclude=exclude)

        def clean(self):
            super().clean()

    return AbstractNode
