from datetime import timedelta
import logging

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import F, Max
from django.utils.encoding import force_text
from django.utils.timezone import now

from mayan.apps.common.classes import ModelQueryFields

from .settings import (
    setting_favorite_count, setting_recent_access_count,
    setting_recent_created_document_count, setting_stub_expiration_interval
)

logger = logging.getLogger(name=__name__)


class DocumentManager(models.Manager):
    def delete_stubs(self):
        stale_stub_documents = self.filter(
            is_stub=True, datetime_created__lt=now() - timedelta(
                seconds=setting_stub_expiration_interval.value
            )
        )
        for stale_stub_document in stale_stub_documents:
            stale_stub_document.delete(to_trash=False)

    def get_by_natural_key(self, uuid):
        return self.get(uuid=force_text(uuid))


class DocumentFileManager(models.Manager):
    def get_by_natural_key(self, checksum, document_natural_key):
        Document = apps.get_model(
            app_label='documents', model_name='Document'
        )
        try:
            document = Document.objects.get_by_natural_key(*document_natural_key)
        except Document.DoesNotExist:
            raise self.model.DoesNotExist

        return self.get(document__pk=document.pk, checksum=checksum)


class DocumentFilePageManager(models.Manager):
    def get_by_natural_key(self, page_number, document_file_natural_key):
        DocumentFile = apps.get_model(
            app_label='documents', model_name='DocumentFile'
        )
        try:
            document_file = DocumentFile.objects.get_by_natural_key(*document_file_natural_key)
        except DocumentFile.DoesNotExist:
            raise self.model.DoesNotExist

        return self.get(
            document_file__pk=document_file.pk, page_number=page_number
        )


class DocumentTypeManager(models.Manager):
    def check_delete_periods(self):
        logger.info(msg='Executing')

        for document_type in self.all():
            logger.info(
                'Checking deletion period of document type: %s', document_type
            )
            if document_type.delete_time_period and document_type.delete_time_unit:
                delta = timedelta(
                    **{
                        document_type.delete_time_unit: document_type.delete_time_period
                    }
                )
                logger.info(
                    'Document type: %s, has a deletion period delta of: %s',
                    document_type, delta
                )
                for document in document_type.trashed_documents.filter(trashed_date_time__lt=now() - delta):
                    logger.info(
                        'Document "%s" with id: %d, trashed on: %s, exceeded '
                        'delete period', document, document.pk,
                        document.trashed_date_time
                    )
                    document.delete()
            else:
                logger.info(
                    'Document type: %s, has a no retention delta', document_type
                )

        logger.info(msg='Finished')

    def check_trash_periods(self):
        logger.info(msg='Executing')

        for document_type in self.all():
            logger.info(
                'Checking trash period of document type: %s', document_type
            )
            if document_type.trash_time_period and document_type.trash_time_unit:
                delta = timedelta(
                    **{
                        document_type.trash_time_unit: document_type.trash_time_period
                    }
                )
                logger.info(
                    'Document type: %s, has a trash period delta of: %s',
                    document_type, delta
                )
                for document in document_type.documents.filter(datetime_created__lt=now() - delta):
                    logger.info(
                        'Document "%s" with id: %d, added on: %s, exceeded '
                        'trash period', document, document.pk,
                        document.datetime_created
                    )
                    document.delete()
            else:
                logger.info(
                    'Document type: %s, has a no retention delta', document_type
                )

        logger.info(msg='Finished')

    def get_by_natural_key(self, label):
        return self.get(label=label)


class DuplicatedDocumentManager(models.Manager):
    def clean_empty_duplicate_lists(self):
        self.filter(documents=None).delete()

    def get_duplicated_documents(self):
        Document = apps.get_model(
            app_label='documents', model_name='Document'
        )
        return Document.valid.filter(
            pk__in=self.filter(documents__in_trash=False).values(
                'document_id'
            )
        )

    def get_duplicates_of(self, document):
        Document = apps.get_model(
            app_label='documents', model_name='Document'
        )

        try:
            queryset = self.get(
                document=document
            ).documents.all()

            return Document.valid.filter(
                pk__in=queryset
            )
        except self.model.DoesNotExist:
            return Document.objects.none()

    def scan(self):
        """
        Find duplicates by iterating over all documents and then
        find matching latest files checksums
        """
        Document = apps.get_model(
            app_label='documents', model_name='Document'
        )

        for document in Document.valid.all():
            self.scan_for(document=document, scan_children=False)

    def scan_for(self, document, scan_children=True):
        """
        Find duplicates by matching latest file checksums
        """
        if not document.file_latest:
            return None

        Document = apps.get_model(
            app_label='documents', model_name='Document'
        )

        # Get the documents whose latest file matches the checksum
        # of the current document and exclude the current document

        ###DISABLED
        ##TODO: redefine what is a duplicate
        """
        duplicates = Document.objects.annotate(
            max_timestamp=Max('files__timestamp')
        ).filter(
            files__timestamp=F('max_timestamp'),
            files__checksum=document.checksum
        ).exclude(pk=document.pk)

        if duplicates.exists():
            instance, created = self.get_or_create(document=document)
            instance.documents.add(*duplicates)
        else:
            self.filter(document=document).delete()

        if scan_children:
            for document in duplicates:
                self.scan_for(document=document, scan_children=False)
        """

class FavoriteDocumentManager(models.Manager):
    def add_for_user(self, user, document):
        favorite_document, created = self.model.objects.get_or_create(
            user=user, document=document
        )

        # Delete old (by date) favorites in excess of the values of
        # setting_favorite_count.
        favorites_to_delete = self.filter(user=user).values('pk').order_by(
            '-datetime_added'
        )[setting_favorite_count.value:]
        self.filter(pk__in=favorites_to_delete).delete()

        return favorite_document

    def get_by_natural_key(self, datetime_accessed, document_natural_key, user_natural_key):
        Document = apps.get_model(
            app_label='documents', model_name='Document'
        )
        User = get_user_model()
        try:
            document = Document.objects.get_by_natural_key(*document_natural_key)
        except Document.DoesNotExist:
            raise self.model.DoesNotExist
        else:
            try:
                user = User.objects.get_by_natural_key(*user_natural_key)
            except User.DoesNotExist:
                raise self.model.DoesNotExist

        return self.get(document__pk=document.pk, user__pk=user.pk)

    def get_for_user(self, user):
        Document = apps.get_model(
            app_label='documents', model_name='Document'
        )

        return Document.objects.filter(favorites__user=user)

    def remove_for_user(self, user, document):
        self.get(user=user, document=document).delete()


class RecentlyAccessedDocumentManager(models.Manager):
    def add_document_for_user(self, user, document):
        if user.is_authenticated:
            new_recent, created = self.model.objects.get_or_create(
                user=user, document=document
            )
            if not created:
                # document already in the recent list, just save to force
                # accessed date and time update
                new_recent.save()

            recent_to_delete = self.filter(user=user).values_list('pk', flat=True)[setting_recent_access_count.value:]
            self.filter(pk__in=list(recent_to_delete)).delete()
        return new_recent

    def get_by_natural_key(self, datetime_accessed, document_natural_key, user_natural_key):
        Document = apps.get_model(
            app_label='documents', model_name='Document'
        )
        User = get_user_model()
        try:
            document = Document.objects.get_by_natural_key(*document_natural_key)
        except Document.DoesNotExist:
            raise self.model.DoesNotExist
        else:
            try:
                user = User.objects.get_by_natural_key(*user_natural_key)
            except User.DoesNotExist:
                raise self.model.DoesNotExist

        return self.get(
            document__pk=document.pk, user__pk=user.pk,
            datetime_accessed=datetime_accessed
        )

    def get_for_user(self, user):
        Document = apps.get_model(
            app_label='documents', model_name='Document'
        )

        if user.is_authenticated:
            return Document.objects.filter(
                recent__user=user
            ).order_by('-recent__datetime_accessed')
        else:
            return Document.objects.none()


class RecentlyCreatedDocumentManager(models.Manager):
    def get_queryset(self):
        RecentlyCreatedDocument = apps.get_model(
            app_label='documents', model_name='RecentlyCreatedDocument'
        )

        queryset = ModelQueryFields.get(
            model=RecentlyCreatedDocument
        ).get_queryset()

        return queryset.filter(
            pk__in=queryset.order_by('-datetime_created')[
                :setting_recent_created_document_count.value
            ].values('pk')
        ).order_by('-datetime_created')


class TrashCanManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(in_trash=True)


class TrashCanQuerySet(models.QuerySet):
    def delete(self, to_trash=True):
        for instance in self:
            instance.delete(to_trash=to_trash)


class ValidDocumentManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(
            in_trash=False
        )
