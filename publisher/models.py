from django.utils import timezone
from django.db import models
from django.core.exceptions import ObjectDoesNotExist

from .managers import PublisherManager
from .utils import assert_draft
from .signals import (
    publisher_publish_pre_save_draft,
    publisher_pre_publish,
    publisher_post_publish,
    publisher_pre_unpublish,
    publisher_post_unpublish,
)


class PublisherModelBase(models.Model):
    publisher_linked = models.OneToOneField(
        'self',
        related_name='publisher_draft',
        null=True,
        editable=False,
        on_delete=models.SET_NULL,
    )
    publisher_is_draft = models.BooleanField(
        default=True,
        editable=False,
        db_index=True,
    )
    publisher_is_published = models.BooleanField(
        default=True,
        db_index=True,
    )
    publisher_modified_at = models.DateTimeField(
        default=timezone.now,
        editable=False,
    )

    publisher_published_at = models.DateTimeField(blank=True, null=True)

    publisher_fields = (
        'publisher_linked',
        'publisher_is_draft',
        'publisher_is_published',
        'publisher_modified_at',
        'publisher_draft',
    )
    publisher_ignore_fields = publisher_fields + (
        'pk',
        'id',
        'publisher_linked',
    )
    publisher_publish_empty_fields = (
        'pk',
        'id',
    )

    class Meta:
        abstract = True

    @property
    def is_draft(self):
        return self.publisher_is_draft

    @property
    def is_published(self):
        return self.publisher_is_published

    @property
    def is_dirty(self):
        if not self.is_draft:
            return False

        # If the record has not been published assume dirty
        if not self.publisher_linked:
            return True

        if self.publisher_modified_at > self.publisher_linked.publisher_modified_at:
            return True

        # Get all placeholders + their plugins to find their modified date
        for placeholder_field in self.get_placeholder_fields():
            placeholder = getattr(self, placeholder_field.name)
            for plugin in placeholder.get_plugins_list():
                if plugin.changed_date > self.publisher_linked.publisher_modified_at:
                    return True

        return False

    @assert_draft
    def publish(self):
        if not self.is_draft:
            return

        publisher_pre_publish.send(sender=self.__class__, instance=self)

        # Reference self for readability
        draft_obj = self

        # Set the published date if this is the first time the page has been published
        if not draft_obj.publisher_linked:
            if draft_obj.publisher_published_at is None:
                draft_obj.publisher_published_at = timezone.now()

        # Duplicate the draft object and set to published
        publish_obj = self.__class__.objects.get(pk=self.pk)
        for fld in self.publisher_publish_empty_fields:
            setattr(publish_obj, fld, None)
        publish_obj.publisher_is_draft = False
        publish_obj.publisher_published_at = draft_obj.publisher_published_at

        # Link the published obj to the draft version
        # publish_obj.publisher_linked = draft_obj
        # publish_obj.save()

        # Check for translations, if so duplicate the object
        self.clone_translations(draft_obj, publish_obj)

        # Clone any placeholder fields into the new published object
        self.clone_placeholder(draft_obj, publish_obj)

        # Clone relationships
        self.clone_relations(draft_obj, publish_obj)

        # Link the draft obj to the current published version
        draft_obj.publisher_linked = publish_obj

        publisher_publish_pre_save_draft.send(sender=draft_obj.__class__, instance=draft_obj)

        draft_obj.save(suppress_modified=True)

        publisher_post_publish.send(sender=draft_obj.__class__, instance=draft_obj)

    @assert_draft
    def patch_placeholders(self, draft_obj):
        try:
            from cms.utils.copy_plugins import copy_plugins_to  # noqa
        except ImportError:
            return

        published_obj = draft_obj.publisher_linked

        for field in self.get_placeholder_fields(draft_obj):
            draft_placeholder = getattr(draft_obj, field.name)
            published_placeholder = getattr(published_obj, field.name)

            if draft_placeholder.pk == published_placeholder.pk:
                published_placeholder.pk = None
                published_placeholder.save()

    @assert_draft
    def unpublish(self):
        if not self.is_draft or not self.publisher_linked:
            return

        publisher_pre_unpublish.send(sender=self.__class__, instance=self)
        self.publisher_linked.publisher_is_published = False
        self.save()
        publisher_post_unpublish.send(sender=self.__class__, instance=self)


    def get_unique_together(self):
        return self._meta.unique_together

    def get_field(self, field_name):
        # return the actual field (not the db representation of the field)
        try:
            # return self._meta.get_field_by_name(field_name)[0]
            return self._meta.get_field(field_name)
        except models.fields.FieldDoesNotExist:
            return None

    @staticmethod
    def clone_translations(src_obj, dst_obj):
        if hasattr(src_obj, 'translations'):
            for translation in src_obj.translations.all():
                translation.pk = None
                translation.master = dst_obj
                translation.save()

    def clone_placeholder(self, src_obj, dst_obj):
        try:
            from cms.utils.copy_plugins import copy_plugins_to
        except ImportError:
            return

        for field in self.get_placeholder_fields(src_obj):
            src_placeholder = getattr(src_obj, field.name)
            dst_placeholder = getattr(dst_obj, field.name)

            dst_placeholder.pk = None
            dst_placeholder.save()

            setattr(dst_obj, field.name, dst_placeholder)
            dst_obj.save()

            src_plugins = src_placeholder.get_plugins_list()

            # CMS automatically generates a new Placeholder ID
            copy_plugins_to(src_plugins, dst_placeholder)

    def clone_relations(self, src_obj, dst_obj):
        """
        Since copying relations is so complex, leave this to the implementing class
        """
        pass

    def get_placeholder_fields(self, obj=None):
        placeholder_fields = []

        try:
            from cms.models.placeholdermodel import Placeholder
        except ImportError:
            return placeholder_fields

        if obj is None:
            obj = self

        model_fields = obj.__class__._meta.get_fields()

        for field in model_fields:
            if field in self.publisher_ignore_fields:
                continue

            try:
                placeholder = getattr(obj, field.name)
                if isinstance(placeholder, Placeholder):
                    placeholder_fields.append(field)
            except (ObjectDoesNotExist, AttributeError):
                continue

        return placeholder_fields

    def update_modified_at(self):
        self.publisher_modified_at = timezone.now()


class PublisherModel(PublisherModelBase):
    objects = models.Manager()
    publisher_manager = PublisherManager()

    class Meta:
        abstract = True
        permissions = (
            ('can_publish', 'Can publish'),
        )

    def save(self, suppress_modified=False, **kwargs):
        if suppress_modified is False:
            self.update_modified_at()

        super(PublisherModel, self).save(**kwargs)
